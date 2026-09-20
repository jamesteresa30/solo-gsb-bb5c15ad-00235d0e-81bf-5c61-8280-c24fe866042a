"""冻融引擎纯函数单元测试。"""

import pytest

from app.engine import (
    ALERT_CYCLE_LIMIT,
    ALERT_EXPOSURE_BUDGET,
    ALERT_TEMPERATURE_EXCURSION,
    EXPOSURE_SIDE_HIGH,
    EXPOSURE_SIDE_LOW,
    STATE_FROZEN,
    STATE_THAWED,
    DerivedState,
    EngineConfig,
    EngineReading,
    alert_dedup_key,
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


# ---------------- 累计温度暴露剂量 ----------------


def test_low_exposure_trapezoid_integral() -> None:
    # 整段低于下限：平均超限幅度 (10+30)/2=20，时长 30min -> 600 °C·min
    readings = [
        _r(1, "2026-09-20T08:00:00+00:00", -90.0),
        _r(2, "2026-09-20T08:30:00+00:00", -110.0),
    ]
    state = derive(readings, _config(min_allowed_c=-80, max_allowed_c=8))
    assert state.low_exposure_c_min == pytest.approx(600.0)
    assert state.high_exposure_c_min == pytest.approx(0.0)
    assert state.exposure_gap_minutes == 0.0


def test_exposure_threshold_crossing_split_by_interpolation() -> None:
    # 从区间内线性越下限：穿越点在 20/30 处（08:20），越限三角形
    # 底 10min、高 10°C -> 50 °C·min
    readings = [
        _r(1, "2026-09-20T08:00:00+00:00", -60.0),
        _r(2, "2026-09-20T08:30:00+00:00", -90.0),
    ]
    state = derive(readings, _config(min_allowed_c=-80, max_allowed_c=8))
    assert state.low_exposure_c_min == pytest.approx(50.0)

    # 回到区间内：-70 距下限 10°C，第二段梯形 (10+10)/2*30 = 75；合计 125
    readings.append(_r(3, "2026-09-20T09:00:00+00:00", -70.0))
    state = derive(readings, _config(min_allowed_c=-80, max_allowed_c=8))
    assert state.low_exposure_c_min == pytest.approx(125.0)


def test_high_exposure_integral() -> None:
    # 10 -> 16，上限 8：越限幅度 2 -> 8，30min 梯形 -> 150 °C·min
    readings = [
        _r(1, "2026-09-20T08:00:00+00:00", 10.0),
        _r(2, "2026-09-20T08:30:00+00:00", 16.0),
    ]
    state = derive(readings, _config(min_allowed_c=-80, max_allowed_c=8))
    assert state.high_exposure_c_min == pytest.approx(150.0)
    assert state.low_exposure_c_min == pytest.approx(0.0)


def test_gap_interval_is_skipped_and_counted() -> None:
    readings = [
        _r(1, "2026-09-20T08:00:00+00:00", -90.0),
        # 61 分钟 > 上限 60：跳过且计缺口 61
        _r(2, "2026-09-20T09:01:00+00:00", -90.0),
        # 随后 30 分钟正常积分：10°C * 30min = 300
        _r(3, "2026-09-20T09:31:00+00:00", -90.0),
    ]
    state = derive(
        readings,
        _config(min_allowed_c=-80, max_integration_gap_minutes=60),
    )
    assert state.low_exposure_c_min == pytest.approx(300.0)
    assert state.exposure_gap_minutes == pytest.approx(61.0)

    # 间隔恰好等于上限（60min）仍然积分
    state_eq = derive(
        readings[:2],
        _config(min_allowed_c=-80, max_integration_gap_minutes=61),
    )
    assert state_eq.exposure_gap_minutes == 0.0
    assert state_eq.low_exposure_c_min == pytest.approx(610.0)


def test_budget_alert_fires_once_per_side_with_stable_key() -> None:
    # 两段各 300，预算 500：只在第二段首次跨越一次
    readings = [
        _r(1, "2026-09-20T08:00:00+00:00", -90.0),
        _r(2, "2026-09-20T08:30:00+00:00", -90.0),
        _r(3, "2026-09-20T09:00:00+00:00", -90.0),
    ]
    cfg = _config(
        min_allowed_c=-80,
        low_exposure_budget_c_min=500,
        high_exposure_budget_c_min=9999,
    )
    state = derive(readings, cfg)
    budget_alerts = [a for a in state.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]
    assert len(budget_alerts) == 1
    alert = budget_alerts[0]
    assert alert.exposure_side == EXPOSURE_SIDE_LOW
    assert alert.severity == "warning"
    assert alert.exposure_budget_c_min == 500
    # 跨越时间在第二段内按剂量线性插值：(500-300)/300 = 2/3 -> 08:50Z
    assert alert.event_time == "2026-09-20T08:50:00+00:00"
    # 稳定去重键只依赖样本与方向
    assert alert_dedup_key("s1", alert) == "s1:exposure_budget:low"


def test_budget_alert_is_reversible_when_dose_shrinks() -> None:
    cfg = _config(min_allowed_c=-80, low_exposure_budget_c_min=200)
    # 60min × 10°C = 600，超过预算
    over = [
        _r(1, "2026-09-20T08:00:00+00:00", -90.0),
        _r(2, "2026-09-20T09:00:00+00:00", -90.0),
    ]
    assert any(
        a.alert_type == ALERT_EXPOSURE_BUDGET for a in derive(over, cfg).alerts
    )

    # 迟到读数改写曲线：08:30 回到区间内 -70（高于下限 -80），
    # 两个穿越三角形越限段各 15min、高 10°C -> 各 75，总量 150 < 200
    # -> 告警消失（可逆）
    with_late = [
        over[0],
        _r(3, "2026-09-20T08:30:00+00:00", -70.0),
        over[1],
    ]
    state = derive(with_late, cfg)
    assert state.low_exposure_c_min == pytest.approx(150.0)
    assert not [a for a in state.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]

    # 删除迟到读数（恢复原曲线）后剂量重回 600，告警重新成立
    again = derive(over, cfg)
    assert again.low_exposure_c_min == pytest.approx(600.0)
    assert [a for a in again.alerts if a.alert_type == ALERT_EXPOSURE_BUDGET]


def test_low_and_high_budgets_each_fire_once() -> None:
    # 构造四段：先低后高。低温两侧三角形各 16.67（越限段 10/3min），
    # 低剂量合计 33.33；高温两侧三角形各 83.33（越限段 50/3min），
    # 高剂量合计 166.67。
    readings = [
        _r(1, "2026-09-20T08:00:00+00:00", 0.0),
        _r(2, "2026-09-20T08:30:00+00:00", -90.0),
        _r(3, "2026-09-20T09:00:00+00:00", 0.0),
        _r(4, "2026-09-20T09:30:00+00:00", 18.0),
        _r(5, "2026-09-20T10:00:00+00:00", 0.0),
    ]
    cfg = _config(
        min_allowed_c=-80,
        max_allowed_c=8,
        low_exposure_budget_c_min=25,
        high_exposure_budget_c_min=120,
    )
    state = derive(readings, cfg)
    sides = sorted(
        a.exposure_side
        for a in state.alerts
        if a.alert_type == ALERT_EXPOSURE_BUDGET
    )
    assert sides == [EXPOSURE_SIDE_HIGH, EXPOSURE_SIDE_LOW]
    assert state.low_exposure_c_min == pytest.approx(33.333333)
    assert state.high_exposure_c_min == pytest.approx(166.666667)


def test_no_dose_without_allowed_bounds() -> None:
    # 未配置允许区间时不计算任何方向剂量
    readings = [
        _r(1, "2026-09-20T08:00:00+00:00", -200.0),
        _r(2, "2026-09-20T08:30:00+00:00", 200.0),
    ]
    state = derive(readings, _config())
    assert state.low_exposure_c_min == 0.0
    assert state.high_exposure_c_min == 0.0
    assert state.exposure_gap_minutes == 0.0
