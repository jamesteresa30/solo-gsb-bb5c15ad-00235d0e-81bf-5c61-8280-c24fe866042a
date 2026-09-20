"""冻融引擎纯函数单元测试。"""

from app.engine import (
    ALERT_CYCLE_LIMIT,
    ALERT_EXPOSURE_BUDGET,
    ALERT_TEMPERATURE_EXCURSION,
    DIRECTION_COLD,
    DIRECTION_HEAT,
    STATE_FROZEN,
    STATE_THAWED,
    DerivedState,
    EngineConfig,
    EngineReading,
    derive,
)


def _r(i: int, t: str, temp: float) -> EngineReading:
    return EngineReading(id=i, event_time=t, temperature_c=temp)


def _t(i: int, minute: float, temp: float) -> EngineReading:
    """以 2026-09-20T08:00:00Z 为基准、偏移 ``minute`` 分钟构造读数。"""

    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)
    event_time = (base + timedelta(minutes=minute)).isoformat()
    return EngineReading(id=i, event_time=event_time, temperature_c=temp)


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
        _r(1, "2026-09-20T08:00:00+00:00", -85.0),  # alert
        _r(2, "2026-09-20T08:30:00+00:00", -90.0),  # 连续越限，无新告警
        _r(3, "2026-09-20T09:00:00+00:00", -10.0),  # 回区间
        _r(4, "2026-09-20T09:30:00+00:00", 12.0),   # 超上限，再告警
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


# ---------------- 累计温度暴露剂量 ----------------


def test_constant_cold_exposure_dose() -> None:
    # 恒温 -85°C 持续 60 分钟，下限 -80：幅度 5 × 60 = 300°C·min
    cfg = _config(min_allowed_c=-80, cold_dose_budget_c_min=100)
    state = derive([_t(1, 0, -85.0), _t(2, 60, -85.0)], cfg)
    assert state.cold_dose_c_min == 300.0
    assert state.heat_dose_c_min == 0.0
    assert state.gap_minutes == 0.0


def test_linear_ramp_split_at_threshold() -> None:
    # -85 -> -75 一小时：前 30 分钟低于 -80，幅度从 5 线性降到 0
    # 三角面积 = (5 + 0) / 2 × 30 = 75
    cfg = _config(min_allowed_c=-80)
    state = derive([_t(1, 0, -85.0), _t(2, 60, -75.0)], cfg)
    assert state.cold_dose_c_min == 75.0
    assert state.heat_dose_c_min == 0.0


def test_segment_entirely_inside_band_is_zero() -> None:
    cfg = _config(min_allowed_c=-80, max_allowed_c=8)
    state = derive([_t(1, 0, 4.0), _t(2, 30, -10.0), _t(3, 60, 7.0)], cfg)
    assert state.cold_dose_c_min == 0.0
    assert state.heat_dose_c_min == 0.0


def test_bidirectional_doses_accumulated_separately() -> None:
    # -10 -> 20，30 分钟，区间 [0, 10]
    # f=1/3（10 分钟）处越过 0，f=2/3（20 分钟）处越过 10：
    # 冷侧正值段 10 分钟、幅度 10 -> 0：0.5 × 10 × 10 = 50
    # 热侧正值段 10 分钟、幅度 0 -> 10：0.5 × 10 × 10 = 50
    cfg = _config(min_allowed_c=0, max_allowed_c=10)
    state = derive([_t(1, 0, -10.0), _t(2, 30, 20.0)], cfg)
    assert state.cold_dose_c_min == 50.0
    assert state.heat_dose_c_min == 50.0


def test_interval_over_max_is_skipped_and_counted_as_gap() -> None:
    cfg = _config(max_allowed_c=10, max_integration_interval_minutes=60)
    state = derive(
        [
            _t(1, 0, 20.0),
            _r(2, "2026-09-20T09:01:00+00:00", 20.0),  # 间隔 61 分钟
            _r(3, "2026-09-20T09:31:00+00:00", 20.0),  # 间隔 30 分钟，正常积分
        ],
        cfg,
    )
    assert state.gap_minutes == 61.0
    assert state.heat_dose_c_min == 300.0  # 仅最后一段 10 × 30


def test_interval_equal_to_max_is_integrated() -> None:
    # 恰好等于上限（非“超过”）的间隔仍参与积分
    cfg = _config(max_allowed_c=10, max_integration_interval_minutes=60)
    state = derive([_t(1, 0, 20.0), _r(2, "2026-09-20T09:00:00+00:00", 20.0)], cfg)
    assert state.gap_minutes == 0.0
    assert state.heat_dose_c_min == 600.0


def test_endpoint_exactly_on_boundary_has_zero_area() -> None:
    cfg = _config(max_allowed_c=10, heat_dose_budget_c_min=1)
    state = derive([_t(1, 0, 5.0), _t(2, 30, 10.0)], cfg)
    assert state.heat_dose_c_min == 0.0
    assert not [a for a in state.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]


def test_budget_alert_fires_once_at_interpolated_crossing() -> None:
    # 恒温 -85（幅度 5）：预算 100 在 20 分钟处跨越，温度仍为 -85
    cfg = _config(min_allowed_c=-80, cold_dose_budget_c_min=100)
    state = derive(
        [_t(1, 0, -85.0), _t(2, 60, -85.0), _t(3, 120, -85.0)],
        cfg,
    )
    budget_alerts = [a for a in state.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]
    assert len(budget_alerts) == 1
    alert = budget_alerts[0]
    assert alert.direction == DIRECTION_COLD
    assert alert.severity == "critical"
    assert alert.event_time == "2026-09-20T08:20:00+00:00"
    assert alert.temperature_c == -85.0
    assert alert.dose_c_min == 100
    assert alert.reading_id == 2  # 跨越发生在 id=1 -> id=2 区间，锚定终点读数


def test_budget_alert_inside_ramping_segment() -> None:
    # 0 -> 20 一小时，上界 10：30 分钟后开始越限，幅度 0 -> 10（热面积共 150）
    # 预算 30：面积公式 10/2 * ((frac-0.5)/0.5)^2 * 30 = 30 -> frac ≈ 0.723607
    cfg = _config(max_allowed_c=10, heat_dose_budget_c_min=30)
    state = derive([_t(1, 0, 0.0), _t(2, 60, 20.0)], cfg)
    alert = next(a for a in state.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET)
    assert alert.direction == DIRECTION_HEAT
    assert alert.event_time == "2026-09-20T08:43:24.984472+00:00"
    assert abs(alert.temperature_c - 14.472136) < 1e-5


def test_budget_alert_dedup_key_is_stable_and_directional() -> None:
    from app.engine import alert_dedup_key

    cfg = _config(
        min_allowed_c=0,
        max_allowed_c=10,
        cold_dose_budget_c_min=10,
        heat_dose_budget_c_min=10,
    )
    state = derive([_t(1, 0, -10.0), _t(2, 30, 20.0)], cfg)
    keys = {
        alert_dedup_key("s1", a)
        for a in state.alerts
        if a.alert_type == ALERT_EXPOSURE_BUDGET
    }
    assert keys == {"s1:exposure_budget:cold", "s1:exposure_budget:heat"}


def test_budget_alert_reversible_on_recompute() -> None:
    readings = [_t(1, 0, -85.0), _t(2, 60, -85.0)]

    under = derive(readings, _config(min_allowed_c=-80, cold_dose_budget_c_min=10_000))
    assert not [a for a in under.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]

    fired = derive(readings, _config(min_allowed_c=-80, cold_dose_budget_c_min=100))
    assert [a for a in fired.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]

    # 预算再次放宽，告警消失（重算可逆）
    cleared = derive(readings, _config(min_allowed_c=-80, cold_dose_budget_c_min=10_000))
    assert not [a for a in cleared.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]


def test_exposure_dose_stable_under_late_reading_reorder() -> None:
    ordered = [
        _r(1, "2026-09-20T08:00:00+00:00", 5.0),
        _r(2, "2026-09-20T08:30:00+00:00", 20.0),
        _r(3, "2026-09-20T09:00:00+00:00", 5.0),
    ]
    cfg = _config(max_allowed_c=10, heat_dose_budget_c_min=100)
    a = derive(ordered, cfg)
    shuffled = [ordered[2], ordered[0], ordered[1]]
    b = derive(sorted(shuffled, key=lambda x: (x.event_time, x.id)), cfg)
    assert a.heat_dose_c_min == b.heat_dose_c_min == 200.0
    alerts_a = [
        (x.direction, x.event_time)
        for x in a.alerts
        if x.alert_type == ALERT_EXPOSURE_BUDGET
    ]
    alerts_b = [
        (x.direction, x.event_time)
        for x in b.alerts
        if x.alert_type == ALERT_EXPOSURE_BUDGET
    ]
    assert alerts_a == alerts_b


def test_zero_duration_intervals_ignored() -> None:
    cfg = _config(max_allowed_c=10, heat_dose_budget_c_min=100)
    state = derive(
        [
            _t(1, 0, 20.0),
            _t(2, 0, 20.0),  # 同一时刻重复点：零时长
            _t(3, 30, 20.0),
        ],
        cfg,
    )
    assert state.heat_dose_c_min == 300.0
    assert len([a for a in state.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]) == 1


def test_no_bounds_means_no_exposure_tracking() -> None:
    state = derive([_t(1, 0, 999.0), _t(2, 60, -999.0)], _config())
    assert state.cold_dose_c_min == 0.0
    assert state.heat_dose_c_min == 0.0
    assert state.gap_minutes == 0.0


def test_gap_counted_without_allowed_bounds() -> None:
    # 未配置允许区间时剂量恒为 0，但最大可积分间隔的缺口统计仍应生效
    cfg = _config(max_integration_interval_minutes=60)
    state = derive(
        [
            _t(1, 0, 999.0),
            _r(2, "2026-09-20T09:01:00+00:00", -999.0),
        ],
        cfg,
    )
    assert state.cold_dose_c_min == 0.0
    assert state.heat_dose_c_min == 0.0
    assert state.gap_minutes == 61.0
