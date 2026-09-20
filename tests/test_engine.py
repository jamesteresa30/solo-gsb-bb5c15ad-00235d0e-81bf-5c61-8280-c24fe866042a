"""冻融引擎纯函数单元测试。"""

from app.engine import (
    ALERT_CYCLE_LIMIT,
    ALERT_TEMPERATURE_EXCURSION,
    STATE_FROZEN,
    STATE_THAWED,
    DerivedState,
    EngineConfig,
    EngineReading,
    derive,
)


def _r(i: int, t: str, temp: float) -> EngineReading:
    return EngineReading(id=i, event_time=t, temperature_c=temp)


def _config(**kw) -> EngineConfig:
    defaults = dict(freeze_threshold_c=-20.0, thaw_threshold_c=0.0)
    defaults.update(kw)
    return EngineConfig(**defaults)


def test_threshold_boundaries_and_hysteresis() -> None:
    # -20 恰好触发冻结；-19.9 不触发；冻结后 0 才解冻，-1 不触发
    readings = [
        _r(1, "t1", -20.0),   # -> frozen
        _r(2, "t2", -19.9),   # 仍 frozen（未达 thaw）
        _r(3, "t3", -1.0),    # 仍 frozen（滞回区）
        _r(4, "t4", 0.0),     # -> thawed，循环 1
    ]
    state = derive(readings, _config())
    assert state.current_state == STATE_THAWED
    assert state.cycle_count == 1
    c = state.cycles[0]
    assert c.freeze_event_time == "t1"
    assert c.thaw_event_time == "t4"
    assert c.min_temperature_c == -20.0
    assert c.max_temperature_c == 0.0


def test_no_flapping_in_deadband_when_thawed() -> None:
    # 已解冻状态下 -10 处于滞回区，不翻转
    state = derive([_r(1, "t1", 4.0), _r(2, "t2", -10.0)], _config())
    assert state.current_state == STATE_THAWED
    assert state.cycle_count == 0


def test_incomplete_freeze_is_not_a_cycle() -> None:
    state = derive(
        [_r(1, "t1", 4.0), _r(2, "t2", -25.0), _r(3, "t3", -30.0)],
        _config(),
    )
    assert state.current_state == STATE_FROZEN
    assert state.cycle_count == 0
    assert state.active_freeze_event_time == "t2"
    assert state.active_freeze_min_c == -30.0
    assert state.active_freeze_max_c == -25.0


def test_three_cycles_chained() -> None:
    temps = [-25, 5, -25, 5, -25, 5]
    readings = [_r(i, f"t{i}", t) for i, t in enumerate(temps, start=1)]
    state = derive(readings, _config())
    assert state.cycle_count == 3
    assert [c.cycle_index for c in state.cycles] == [1, 2, 3]


def test_temperature_excursion_alerts_debounced() -> None:
    # 连续低于下限只告警一次；回到区间后再次越限再告警
    readings = [
        _r(1, "t1", -85.0),  # alert
        _r(2, "t2", -90.0),  # 连续越限，无新告警
        _r(3, "t3", -10.0),  # 回区间
        _r(4, "t4", 12.0),   # 超上限，再告警
    ]
    state = derive(readings, _config(min_allowed_c=-80, max_allowed_c=8))
    types = [a.alert_type for a in state.alerts]
    assert types == [
        ALERT_TEMPERATURE_EXCURSION,
        ALERT_TEMPERATURE_EXCURSION,
    ]
    assert state.alerts[0].reading_id == 1
    assert state.alerts[1].reading_id == 4


def test_cycle_limit_alert_fires_once_per_exceeded_cycle() -> None:
    temps = [-25, 5, -25, 5, -25, 5]
    readings = [_r(i, f"t{i}", t) for i, t in enumerate(temps, start=1)]
    state = derive(readings, _config(max_freeze_thaw_cycles=2))
    limit_alerts = [a for a in state.alerts if a.alert_type == ALERT_CYCLE_LIMIT]
    assert len(limit_alerts) == 1
    assert limit_alerts[0].cycle_index == 3
    assert limit_alerts[0].event_time == "t6"


def test_late_data_does_not_change_engine_contract() -> None:
    # 同一组读数，乱序喂入 vs 排序后喂入，调用方负责排序时结果应一致
    ordered = [
        _r(1, "t1", 4.0),
        _r(2, "t2", -25.0),
        _r(3, "t3", 3.0),
    ]
    shuffled = [ordered[2], ordered[0], ordered[1]]
    a: DerivedState = derive(sorted(ordered, key=lambda x: x.event_time), _config())
    b: DerivedState = derive(sorted(shuffled, key=lambda x: x.event_time), _config())
    assert a.cycle_count == b.cycle_count == 1
    assert a.cycles[0].freeze_event_time == b.cycles[0].freeze_event_time == "t2"
