"""冻融循环识别与累计温度暴露剂量引擎（纯函数，不依赖数据库与 Web 框架）。

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

累计温度暴露剂量（单位 °C·分钟）：

* 对按 ``event_time`` 排序后的**相邻读数做线性插值**，把每段折线在
  ``min_allowed_c`` 以下 / ``max_allowed_c`` 以上的部分切分出来；
* 超低温剂量 = 越下限幅度 (``min_allowed_c - temp``) 对分钟的积分，
  超高温剂量 = 越上限幅度 (``temp - max_allowed_c``) 对分钟的积分；
* 两读数间隔超过 ``max_integration_gap_minutes`` 时该段不积分
  （视为数据缺口），间隔时长计入 ``exposure_gap_minutes``；
* 低温/高温剂量首次分别达到 ``low/high_exposure_budget_c_min`` 预算时，
  各产生一条稳定去重的 ``exposure_budget_exceeded`` 告警；
* 全部结果随读数重放推导，因此迟到数据、阈值/预算变更都会让剂量与告警
  可逆更新（由业务层按 dedup_key upsert / 删除陈旧告警）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

STATE_FROZEN = "frozen"
STATE_THAWED = "thawed"

ALERT_TEMPERATURE_EXCURSION = "temperature_excursion"
ALERT_CYCLE_LIMIT = "cycle_limit_exceeded"
ALERT_EXPOSURE_BUDGET = "exposure_budget_exceeded"

EXPOSURE_SIDE_LOW = "low"
EXPOSURE_SIDE_HIGH = "high"

# 浮点累计结果统一保留的小数位，避免二进制误差影响预算比较
DOSE_ROUND_DIGITS = 6


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
    exposure_side: Optional[str] = None
    exposure_c_min: Optional[float] = None
    exposure_budget_c_min: Optional[float] = None


@dataclass
class EngineConfig:
    freeze_threshold_c: float
    thaw_threshold_c: float
    min_allowed_c: Optional[float] = None
    max_allowed_c: Optional[float] = None
    max_freeze_thaw_cycles: Optional[int] = None
    # 超低温（低于 min_allowed_c）累计剂量预算，单位 °C·分钟
    low_exposure_budget_c_min: Optional[float] = None
    # 超高温（高于 max_allowed_c）累计剂量预算，单位 °C·分钟
    high_exposure_budget_c_min: Optional[float] = None
    # 相邻读数允许积分的最大间隔（分钟）；超过则跳过该段并计为缺口
    max_integration_gap_minutes: Optional[float] = None


@dataclass
class DerivedState:
    current_state: str = STATE_THAWED
    cycle_count: int = 0
    active_freeze_event_time: Optional[str] = None
    active_freeze_min_c: Optional[float] = None
    active_freeze_max_c: Optional[float] = None
    # 累计超低温 / 超高温暴露剂量（°C·分钟）
    low_exposure_c_min: float = 0.0
    high_exposure_c_min: float = 0.0
    # 因间隔超过上限而未积分的缺口总时长（分钟）
    exposure_gap_minutes: float = 0.0
    cycles: list[CycleRecord] = field(default_factory=list)
    alerts: list[AlertRecord] = field(default_factory=list)


def _parse_time(value: str) -> Optional[datetime]:
    """解析内部 UTC ISO 8601 字符串；无法解析（如占位时间）返回 None。"""

    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clipped_linear_integral(
    t0: float, t1: float, y0: float, y1: float
) -> float:
    """线性段 (t0,y0)->(t1,y1) 的非负截断面积（越限幅度 × 时长）。

    调用方按越限方向传入有符号幅度（越限侧为正、区间内为负）：
      * 低温侧传 ``min_allowed_c - temp``（越下限为正）；
      * 高温侧传 ``temp - max_allowed_c``（越上限为正）。
    阈值穿越点按线性插值切分，穿越后的三角形/梯形面积即该段剂量。
    """

    dt = t1 - t0
    if dt <= 0:
        return 0.0
    if y0 <= 0 and y1 <= 0:
        return 0.0
    if y0 > 0 and y1 > 0:
        return (y0 + y1) * 0.5 * dt
    # 恰好穿越 0：越限部分为三角形
    # 穿越点距 t0 的比例 c = y0/(y0-y1)；面积 = 1/2 * 越限峰值 * 越限侧时长
    if y0 > 0:
        crossing = y0 / (y0 - y1)
        return 0.5 * y0 * crossing * dt
    crossing = y0 / (y0 - y1)
    return 0.5 * y1 * (1.0 - crossing) * dt


def _budget_crossing_time(
    prev_time: datetime,
    next_time: datetime,
    start_dose: float,
    segment_dose: float,
    budget: float,
) -> str:
    """预算在本段内被首次跨越的时间：按段内剂量线性插值估计。"""

    if segment_dose <= 0:
        fraction = 0.0
    else:
        fraction = (budget - start_dose) / segment_dose
    fraction = min(1.0, max(0.0, fraction))
    crossed = prev_time + (next_time - prev_time) * fraction
    return crossed.isoformat()


def derive(readings: list[EngineReading], config: EngineConfig) -> DerivedState:
    """按时间顺序处理读数，推导当前状态、完整循环、暴露剂量与告警。

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

    # 暴露剂量累计
    low_dose = 0.0
    high_dose = 0.0
    gap_minutes = 0.0
    low_budget = (
        config.low_exposure_budget_c_min
        if config.low_exposure_budget_c_min is not None
        and config.low_exposure_budget_c_min > 0
        else None
    )
    high_budget = (
        config.high_exposure_budget_c_min
        if config.high_exposure_budget_c_min is not None
        and config.high_exposure_budget_c_min > 0
        else None
    )
    low_budget_fired = low_budget is None
    high_budget_fired = high_budget is None

    prev: Optional[EngineReading] = None
    prev_dt: Optional[datetime] = None

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

        # 2) 累计暴露剂量：与上一条读数做线性插值积分
        cur_dt = _parse_time(r.event_time)
        if prev is not None and prev_dt is not None and cur_dt is not None:
            minutes = (cur_dt - prev_dt).total_seconds() / 60.0
            if minutes < 0:
                # 排序异常保护：不积分
                pass
            elif minutes == 0:
                # 同时刻读数无间隔可积
                pass
            elif (
                config.max_integration_gap_minutes is not None
                and minutes > config.max_integration_gap_minutes
            ):
                # 间隔超过上限：跳过该段，计入缺口时长
                gap_minutes += minutes
            else:
                seg_low = 0.0
                seg_high = 0.0
                if config.min_allowed_c is not None:
                    seg_low = _clipped_linear_integral(
                        0.0,
                        minutes,
                        config.min_allowed_c - prev.temperature_c,
                        config.min_allowed_c - temp,
                    )
                if config.max_allowed_c is not None:
                    seg_high = _clipped_linear_integral(
                        0.0,
                        minutes,
                        prev.temperature_c - config.max_allowed_c,
                        temp - config.max_allowed_c,
                    )

                # 首次跨越预算：同一插值段内低/高温各最多触发一次，
                # 同段同时跨越时低温优先（事件顺序保持确定）。
                if seg_low > 0 and not low_budget_fired:
                    budget = low_budget
                    if low_dose + seg_low >= budget:
                        crossing = _budget_crossing_time(
                            prev_dt, cur_dt, low_dose, seg_low, budget
                        )
                        state.alerts.append(
                            AlertRecord(
                                alert_type=ALERT_EXPOSURE_BUDGET,
                                severity="warning",
                                event_time=crossing,
                                reading_id=r.id,
                                exposure_side=EXPOSURE_SIDE_LOW,
                                exposure_c_min=round(
                                    low_dose + seg_low, DOSE_ROUND_DIGITS
                                ),
                                exposure_budget_c_min=budget,
                                message=(
                                    f"超低温累计暴露剂量 {low_dose + seg_low:.3f}°C·分钟 "
                                    f"已达预算 {budget}°C·分钟"
                                ),
                            )
                        )
                        low_budget_fired = True
                if seg_high > 0 and not high_budget_fired:
                    budget = high_budget
                    if high_dose + seg_high >= budget:
                        crossing = _budget_crossing_time(
                            prev_dt, cur_dt, high_dose, seg_high, budget
                        )
                        state.alerts.append(
                            AlertRecord(
                                alert_type=ALERT_EXPOSURE_BUDGET,
                                severity="warning",
                                event_time=crossing,
                                reading_id=r.id,
                                exposure_side=EXPOSURE_SIDE_HIGH,
                                exposure_c_min=round(
                                    high_dose + seg_high, DOSE_ROUND_DIGITS
                                ),
                                exposure_budget_c_min=budget,
                                message=(
                                    f"超高温累计暴露剂量 {high_dose + seg_high:.3f}°C·分钟 "
                                    f"已达预算 {budget}°C·分钟"
                                ),
                            )
                        )
                        high_budget_fired = True

                low_dose += seg_low
                high_dose += seg_high

        # 3) 滞回状态机
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

        prev = r
        prev_dt = cur_dt

    state.cycle_count = len(state.cycles)
    state.low_exposure_c_min = round(low_dose, DOSE_ROUND_DIGITS)
    state.high_exposure_c_min = round(high_dose, DOSE_ROUND_DIGITS)
    state.exposure_gap_minutes = round(gap_minutes, DOSE_ROUND_DIGITS)
    if state.current_state == STATE_FROZEN:
        state.active_freeze_event_time = freeze_started_at
        state.active_freeze_min_c = seg_min
        state.active_freeze_max_c = seg_max

    return state


def alert_dedup_key(sample_id: str, alert: AlertRecord) -> str:
    """告警在同一样本内的稳定去重键。

    重算时读数 id 与循环序号保持稳定，因此历史告警（含已确认标记）
    可以被精确复用而不会重复产生。暴露预算告警每侧最多一条，
    键只依赖样本与越限方向，迟到数据/配置变更后仍能稳定复用或可逆撤销。
    """

    if alert.alert_type == ALERT_TEMPERATURE_EXCURSION:
        return f"{sample_id}:range:{alert.reading_id}"
    if alert.alert_type == ALERT_CYCLE_LIMIT:
        return f"{sample_id}:cycle_limit:{alert.cycle_index}"
    if alert.alert_type == ALERT_EXPOSURE_BUDGET:
        return f"{sample_id}:exposure_budget:{alert.exposure_side}"
    return f"{sample_id}:{alert.alert_type}:{alert.reading_id}"
