"""冻融循环识别引擎（纯函数，不依赖数据库与 Web 框架）。

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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

STATE_FROZEN = "frozen"
STATE_THAWED = "thawed"

ALERT_TEMPERATURE_EXCURSION = "temperature_excursion"
ALERT_CYCLE_LIMIT = "cycle_limit_exceeded"


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


@dataclass
class EngineConfig:
    freeze_threshold_c: float
    thaw_threshold_c: float
    min_allowed_c: Optional[float] = None
    max_allowed_c: Optional[float] = None
    max_freeze_thaw_cycles: Optional[int] = None


@dataclass
class DerivedState:
    current_state: str = STATE_THAWED
    cycle_count: int = 0
    active_freeze_event_time: Optional[str] = None
    active_freeze_min_c: Optional[float] = None
    active_freeze_max_c: Optional[float] = None
    cycles: list[CycleRecord] = field(default_factory=list)
    alerts: list[AlertRecord] = field(default_factory=list)


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

    return state


def alert_dedup_key(sample_id: str, alert: AlertRecord) -> str:
    """告警在同一样本内的稳定去重键。

    重算时读数 id 与循环序号保持稳定，因此历史告警（含已确认标记）
    可以被精确复用而不会重复产生。
    """

    if alert.alert_type == ALERT_TEMPERATURE_EXCURSION:
        return f"{sample_id}:range:{alert.reading_id}"
    if alert.alert_type == ALERT_CYCLE_LIMIT:
        return f"{sample_id}:cycle_limit:{alert.cycle_index}"
    return f"{sample_id}:{alert.alert_type}:{alert.reading_id}"
