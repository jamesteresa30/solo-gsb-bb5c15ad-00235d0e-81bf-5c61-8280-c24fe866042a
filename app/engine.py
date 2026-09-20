"""冻融循环识别与温度暴露剂量引擎（纯函数，不依赖数据库与 Web 框架）。

规则（带滞回 / Schmitt 触发器）：

* ``THAWED``（已解冻）状态下，温度 <= ``freeze_threshold`` 才进入 ``FROZEN``；
* ``FROZEN``（冻结中）状态下，温度 >= ``thaw_threshold`` 才回到 ``THAWED``，
  并记为一次**完整冻融循环**；
* 要求 ``freeze_threshold < thaw_threshold``，两个阈值之间的温度波动
  不会引起状态翻转，从而避免临界温度附近的抖动；
* 所有读数先按 ``event_time`` 升序（同时间按读数 id）重排后再喂给引擎，
  因此迟到数据会被自动纳入正确的时间位置重新计算；
* 温度越过 ``[min_allowed_c, max_allowed_c]`` 允许区间产生一次越限告警，
  连续越限只告警一次（离开区间后再次越限才会再次告警）；
* 完整循环次数超过 ``max_freeze_thaw_cycles`` 时产生循环超限告警，
  每次超出只告警一次。

累计温度暴露剂量（°C·min）：

* 对相邻读数按 ``event_time`` 做**线性插值**，把曲线越过
  ``min_allowed_c`` / ``max_allowed_c`` 的区段在跨越点切分；
* 分别积分低温方向（低于下限的幅度）与高温方向（高于上限的幅度），
  面积即“超限幅度 × 分钟”；
* 相邻读数间隔超过 ``max_integration_interval_minutes`` 时整个区间
  不参与积分，并把该间隔计入缺口时长（gap minutes）；
* 任一方向累计剂量**首次**达到对应预算（cold/heat dose budget）时，
  产生稳定去重的 ``exposure_budget_exceeded`` 告警（每方向至多一条）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

STATE_FROZEN = "frozen"
STATE_THAWED = "thawed"

ALERT_TEMPERATURE_EXCURSION = "temperature_excursion"
ALERT_CYCLE_LIMIT = "cycle_limit_exceeded"
ALERT_EXPOSURE_BUDGET = "exposure_budget_exceeded"

DIRECTION_COLD = "cold"
DIRECTION_HEAT = "heat"

# 浮点容差：恰好落在阈值上的幅度视为 0，避免 -0.0 与临界抖动误判
_EPS = 1e-9


@dataclass(frozen=True)
class EngineReading:
    """引擎输入的单条读数。``id`` 对应数据库 readings.id，保证稳定可追溯。"""

    id: int
    event_time: str
    temperature_c: float


@dataclass
class CycleRecord:
    cycle_index: int
    freeze_event_time: str
    freeze_temperature_c: float
    thaw_event_time: str
    thaw_temperature_c: float
    min_temperature_c: float
    max_temperature_c: float


@dataclass
class AlertRecord:
    alert_type: str
    severity: str
    event_time: str
    message: str
    reading_id: int
    temperature_c: Optional[float] = None
    cycle_index: Optional[int] = None
    direction: Optional[str] = None
    dose_c_min: Optional[float] = None
    budget_c_min: Optional[float] = None


@dataclass
class EngineConfig:
    freeze_threshold_c: float
    thaw_threshold_c: float
    min_allowed_c: Optional[float] = None
    max_allowed_c: Optional[float] = None
    max_freeze_thaw_cycles: Optional[int] = None
    # 低温/高温累计暴露剂量预算（°C·min）；None 表示不追踪该方向
    cold_dose_budget_c_min: Optional[float] = None
    heat_dose_budget_c_min: Optional[float] = None
    # 相邻读数最大可积分间隔（分钟）；超过则该区间跳过并计入缺口
    max_integration_interval_minutes: Optional[float] = None


@dataclass
class DerivedState:
    current_state: str = STATE_THAWED
    cycle_count: int = 0
    active_freeze_event_time: Optional[str] = None
    active_freeze_min_c: Optional[float] = None
    active_freeze_max_c: Optional[float] = None
    # 累计温度暴露剂量（°C·min）与因间隔过长跳过的缺口总时长（分钟）
    cold_dose_c_min: float = 0.0
    heat_dose_c_min: float = 0.0
    gap_minutes: float = 0.0
    cycles: list[CycleRecord] = field(default_factory=list)
    alerts: list[AlertRecord] = field(default_factory=list)


def _parse_utc(event_time: str) -> datetime:
    """解析内部 UTC ISO 8601 时间（与 services.to_utc_iso 的输出一致）。"""

    dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _positive_piece(e0: float, e1: float) -> Optional[tuple[float, float, float, float]]:
    """把一个线性区间的“超限幅度”正值部分切出来。

    端点幅度 ``e0``/``e1``（允许为负，表示尚在界内）。返回
    ``(fa, fb, sa, sb)``：正值部分在区间内的起止比例（0~1）与对应幅度，
    该正值部分的积分（幅度·分钟）= 梯形面积 ``(sa + sb) / 2 * (fb - fa) * minutes``。
    线性函数在一个区间内至多跨越零点一次，因此正值部分只有一段；无正值返回 None。
    """

    if e0 <= _EPS and e1 <= _EPS:
        return None
    if e0 >= -_EPS and e1 >= -_EPS:
        return 0.0, 1.0, max(e0, 0.0), max(e1, 0.0)
    # 一正一负：按比例切出零点
    frac = e0 / (e0 - e1)
    if e1 > 0:  # 区间内由负转正
        return frac, 1.0, 0.0, e1
    # 区间内由正转负
    return 0.0, frac, e0, 0.0


def _interval_excess(
    t0: datetime,
    temp0: float,
    t1: datetime,
    temp1: float,
    bound: float,
    direction: str,
) -> Optional[tuple[float, float, float, float]]:
    """返回某方向在一个区间上的正值段 ``(fa, fb, sa, sb)``。

    ``direction='cold'`` 时幅度 = ``bound - temp``（低于下限为正）；
    ``'heat'`` 时幅度 = ``temp - bound``。无越限返回 None。
    """

    if direction == DIRECTION_COLD:
        e0, e1 = bound - temp0, bound - temp1
    else:
        e0, e1 = temp0 - bound, temp1 - bound
    return _positive_piece(e0, e1)


def _crossing_fraction(
    fa: float,
    fb: float,
    sa: float,
    sb: float,
    remaining: float,
    minutes: float,
) -> Optional[tuple[float, float]]:
    """在正值段内定位“累计幅度×分钟恰好达到 remaining 预算”的比例。

    段内幅度线性：``s(u) = sa + (sb - sa) * u``，需要求积分等于 remaining 的
    位置，即解关于段内比例 ``u`` 的二次方程。返回
    ``(fraction, cross_temp_excess)``：越界时刻在整个读数区间内的比例
    （0~1）以及该时刻的超限幅度。预算在本区间内不被跨越时返回 None。
    """

    length = (fb - fa) * minutes
    target = remaining / length  # 折算为段内平均幅度目标
    if target < -_EPS:
        return None
    a = sb - sa
    if abs(a) <= _EPS:
        if sa <= _EPS or target > sa + _EPS:
            return None
        u = target / sa
    else:
        # integral_0^u (sa + a*x) dx = sa*u + a*u^2/2 = target
        disc = sa * sa + 2 * a * target
        if disc < -_EPS:
            return None
        u = (-sa + math.sqrt(max(disc, 0.0))) / a
    if u < -_EPS or u > 1 + _EPS:
        return None
    u = min(max(u, 0.0), 1.0)
    cross_excess = sa + (sb - sa) * u
    return fa + (fb - fa) * u, cross_excess


def derive(readings: list[EngineReading], config: EngineConfig) -> DerivedState:
    """按时间顺序处理读数，推导当前状态、完整循环与告警。

    ``readings`` 应由调用方保证已按 (event_time, id) 升序排列。
    """

    state = DerivedState()

    # 冻结段的跟踪数据：起点时间/温度，以及段内最低、最高温度
    freeze_started_at: Optional[str] = None
    freeze_start_temp: Optional[float] = None
    seg_min: Optional[float] = None
    seg_max: Optional[float] = None

    # 是否正处于“允许温度区间之外”，用于越限告警去抖（一次连续越限只告一次）
    outside_allowed = False

    for r in readings:
        temp = r.temperature_c

        # 1) 温度越限告警（与冻融状态机相互独立）
        is_outside = False
        if config.min_allowed_c is not None and temp < config.min_allowed_c:
            is_outside = True
        if config.max_allowed_c is not None and temp > config.max_allowed_c:
            is_outside = True
        if is_outside and not outside_allowed:
            state.alerts.append(
                AlertRecord(
                    alert_type=ALERT_TEMPERATURE_EXCURSION,
                    severity="critical",
                    event_time=r.event_time,
                    reading_id=r.id,
                    temperature_c=temp,
                    message=(
                        f"温度 {temp}°C 超出允许区间 "
                        f"[{config.min_allowed_c if config.min_allowed_c is not None else '-∞'}, "
                        f"{config.max_allowed_c if config.max_allowed_c is not None else '+∞'}]°C"
                    ),
                )
            )
        outside_allowed = is_outside

        # 2) 滞回状态机
        if state.current_state == STATE_THAWED:
            if temp <= config.freeze_threshold_c:
                state.current_state = STATE_FROZEN
                freeze_started_at = r.event_time
                freeze_start_temp = temp
                seg_min = seg_max = temp

        else:  # FROZEN
            seg_min = min(seg_min, temp)
            seg_max = max(seg_max, temp)

            if temp >= config.thaw_threshold_c:
                # 一次完整冻融循环闭环
                cycle_index = len(state.cycles) + 1
                state.cycles.append(
                    CycleRecord(
                        cycle_index=cycle_index,
                        freeze_event_time=freeze_started_at,
                        freeze_temperature_c=freeze_start_temp,
                        thaw_event_time=r.event_time,
                        thaw_temperature_c=temp,
                        min_temperature_c=seg_min,
                        max_temperature_c=seg_max,
                    )
                )

                if (
                    config.max_freeze_thaw_cycles is not None
                    and cycle_index > config.max_freeze_thaw_cycles
                ):
                    state.alerts.append(
                        AlertRecord(
                            alert_type=ALERT_CYCLE_LIMIT,
                            severity="warning",
                            event_time=r.event_time,
                            reading_id=r.id,
                            temperature_c=temp,
                            cycle_index=cycle_index,
                            message=(
                                f"冻融循环次数已达 {cycle_index} 次，"
                                f"超过允许上限 {config.max_freeze_thaw_cycles} 次"
                            ),
                        )
                    )

                state.current_state = STATE_THAWED
                freeze_started_at = None
                freeze_start_temp = None
                seg_min = seg_max = None

    state.cycle_count = len(state.cycles)
    if state.current_state == STATE_FROZEN:
        state.active_freeze_event_time = freeze_started_at
        state.active_freeze_min_c = seg_min
        state.active_freeze_max_c = seg_max

    _accumulate_exposure(state, readings, config)
    return state


def _accumulate_exposure(
    state: DerivedState,
    readings: list[EngineReading],
    config: EngineConfig,
) -> None:
    """按相邻读数线性插值累计双向超限暴露剂量（幅度 × 分钟）。

    区间间隔超过 ``max_integration_interval_minutes`` 时整体跳过并计入
    缺口时长；累计剂量首次达到预算时追加稳定去重的预算告警。
    """

    tracks = []
    if config.min_allowed_c is not None:
        tracks.append(
            (
                DIRECTION_COLD,
                config.min_allowed_c,
                config.cold_dose_budget_c_min,
                "低温暴露剂量",
            )
        )
    if config.max_allowed_c is not None:
        tracks.append(
            (
                DIRECTION_HEAT,
                config.max_allowed_c,
                config.heat_dose_budget_c_min,
                "高温暴露剂量",
            )
        )
    if len(readings) < 2:
        return
    if not tracks and config.max_integration_interval_minutes is None:
        return

    totals = {DIRECTION_COLD: 0.0, DIRECTION_HEAT: 0.0}
    gap_minutes = 0.0
    budget_alerts: list[AlertRecord] = []
    # 每个方向只在“首次”跨越预算时告警一次
    fired = {DIRECTION_COLD: False, DIRECTION_HEAT: False}

    for prev, cur in zip(readings, readings[1:]):
        t0, t1 = _parse_utc(prev.event_time), _parse_utc(cur.event_time)
        minutes = (t1 - t0).total_seconds() / 60.0
        if minutes <= _EPS:
            continue
        if (
            config.max_integration_interval_minutes is not None
            and minutes > config.max_integration_interval_minutes
        ):
            # 间隔过长：该区间不做插值积分，只累计缺口时长
            # （缺口统计与是否配置允许区间相互独立）
            gap_minutes += minutes
            continue

        for direction, bound, budget, label in tracks:
            piece = _interval_excess(
                t0, prev.temperature_c, t1, cur.temperature_c, bound, direction
            )
            if piece is None:
                continue
            fa, fb, sa, sb = piece
            area = (sa + sb) / 2 * (fb - fa) * minutes
            if area <= _EPS:
                continue

            before = totals[direction]
            totals[direction] = before + area

            if budget is not None and not fired[direction] and before < budget - _EPS:
                remaining = budget - before
                cross = _crossing_fraction(fa, fb, sa, sb, remaining, minutes)
                if cross is not None:
                    frac, cross_excess = cross
                    cross_dt = t0 + timedelta(minutes=minutes * frac)
                    cross_time = cross_dt.isoformat()
                    if direction == DIRECTION_COLD:
                        cross_temp = bound - cross_excess
                    else:
                        cross_temp = bound + cross_excess
                    fired[direction] = True
                    budget_alerts.append(
                        AlertRecord(
                            alert_type=ALERT_EXPOSURE_BUDGET,
                            severity="critical",
                            event_time=cross_time,
                            reading_id=cur.id,
                            temperature_c=round(cross_temp, 6),
                            direction=direction,
                            dose_c_min=round(budget, 6),
                            budget_c_min=round(budget, 6),
                            message=(
                                f"{label}已达预算 {round(budget, 6)}°C·min"
                                f"（约 {cross_time}，温度 {round(cross_temp, 4)}°C）"
                            ),
                        )
                    )

    state.cold_dose_c_min = round(totals[DIRECTION_COLD], 6)
    state.heat_dose_c_min = round(totals[DIRECTION_HEAT], 6)
    state.gap_minutes = round(gap_minutes, 6)
    # 预算告警发生在插值时刻，统一按事件时间与既有告警合并排序，保证时间序稳定
    state.alerts.extend(budget_alerts)
    state.alerts.sort(key=lambda a: (a.event_time, a.reading_id or 0))


def alert_dedup_key(sample_id: str, alert: AlertRecord) -> str:
    """告警在同一样本内的稳定去重键。

    重算时读数 id 与循环序号保持稳定，因此历史告警（含已确认标记）
    可以被精确复用而不会重复产生。
    """

    if alert.alert_type == ALERT_TEMPERATURE_EXCURSION:
        return f"{sample_id}:range:{alert.reading_id}"
    if alert.alert_type == ALERT_CYCLE_LIMIT:
        return f"{sample_id}:cycle_limit:{alert.cycle_index}"
    if alert.alert_type == ALERT_EXPOSURE_BUDGET:
        # 每个方向的预算只告警一次；阈值/预算变化后重算仍复用同一去重键
        return f"{sample_id}:exposure_budget:{alert.direction}"
    return f"{sample_id}:{alert.alert_type}:{alert.reading_id}"
