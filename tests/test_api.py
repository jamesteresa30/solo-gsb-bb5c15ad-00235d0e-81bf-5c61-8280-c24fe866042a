"""API 集成测试：通过临时 SQLite 文件覆盖 DB_PATH。"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    import app.database as database
    import app.services as services
    import app.main as main

    importlib.reload(database)
    importlib.reload(services)
    importlib.reload(main)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


def _create_sample(client, sid="s1", **kw):
    payload = {
        "id": sid,
        "name": "测试样本",
        "freeze_threshold_c": -20,
        "thaw_threshold_c": 0,
        "min_allowed_c": -80,
        "max_allowed_c": 8,
        "max_freeze_thaw_cycles": 2,
    }
    payload.update(kw)
    r = client.post("/samples", json=payload)
    assert r.status_code == 201, r.text
    return r


def _submit(client, sid, readings):
    return client.post(f"/samples/{sid}/readings", json={"readings": readings})


def test_health_and_startup_init(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # 建库后应能直接创建样本
    assert _create_sample(client).status_code == 201


def test_invalid_thresholds_rejected(client) -> None:
    r = client.post(
        "/samples",
        json={
            "id": "bad",
            "name": "x",
            "freeze_threshold_c": 5,
            "thaw_threshold_c": -5,
        },
    )
    assert r.status_code == 422


def test_full_cycle_state_and_cycles(client) -> None:
    _create_sample(client)
    r = _submit(
        client,
        "s1",
        [
            {"event_id": "a", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4},
            {"event_id": "b", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22},
            {"event_id": "c", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 2},
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 3
    assert body["current_state"] == "thawed"
    assert body["cycle_count"] == 1

    state = client.get("/samples/s1/state").json()
    assert state["current_state"] == "thawed"
    assert state["cycle_count"] == 1
    assert state["last_temperature_c"] == 2.0

    cycles = client.get("/samples/s1/cycles").json()
    assert len(cycles) == 1
    assert cycles[0]["min_temperature_c"] == -22.0
    assert cycles[0]["max_temperature_c"] == 2.0


def test_late_readings_reordered(client) -> None:
    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4},
            {"event_id": "e2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22},
            # 先到一段解冻
            {"event_id": "e5", "event_time": "2026-09-20T12:00:00Z", "temperature_c": 3},
        ],
    )
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # 迟到：09:00 先解冻（闭环第 1 次循环）、09:30 再冻、10:00 再解冻
    # —— 全部插入 08:30 与 12:00 之间，重排后得到 2 次完整循环
    r = _submit(
        client,
        "s1",
        [
            {"event_id": "late1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 3},
            {"event_id": "late2", "event_time": "2026-09-20T09:30:00Z", "temperature_c": -25},
            {"event_id": "late3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 1},
        ],
    )
    assert r.json()["cycle_count"] == 2

    # 读数查询严格按事件时间排序
    times = [r["event_time"] for r in client.get("/samples/s1/readings").json()]
    assert times == sorted(times)
    assert times[0].startswith("2026-09-20T08:00:00")


def test_idempotent_event_id_and_signature(client) -> None:
    _create_sample(client)
    payload = [
        {"event_id": "x1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4},
    ]
    assert _submit(client, "s1", payload).json()["accepted"] == 1
    again = _submit(client, "s1", payload).json()
    assert again["accepted"] == 0
    assert again["duplicates"] == 1

    # 无 event_id，同时刻同温度也判重
    sig = [{"event_time": "2026-09-20T09:00:00Z", "temperature_c": -21}]
    assert _submit(client, "s1", sig).json()["accepted"] == 1
    assert _submit(client, "s1", sig).json()["duplicates"] == 1


def test_excursion_and_cycle_limit_alerts(client) -> None:
    _create_sample(client)
    # 3 次完整循环 + 一次越限
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -22},
            {"event_id": "2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2},
            {"event_id": "3", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -22},
            {"event_id": "4", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 2},
            {"event_id": "5", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -85.5},
            {"event_id": "6", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2},
        ],
    )
    alerts = client.get("/alerts?sample_id=s1").json()
    types = sorted(a["alert_type"] for a in alerts)
    assert types == ["cycle_limit_exceeded", "temperature_excursion"]

    excursion = next(a for a in alerts if a["alert_type"] == "temperature_excursion")
    assert excursion["temperature_c"] == -85.5

    state = client.get("/samples/s1/state").json()
    assert state["unacknowledged_alert_count"] == 2

    # 确认告警
    r = client.post(f"/alerts/{excursion['id']}/ack", json={"acknowledged": True})
    assert r.json()["acknowledged"] is True
    unacked = client.get("/alerts?acknowledged=false").json()
    assert all(a["id"] != excursion["id"] for a in unacked)

    # 样本级批量确认
    n = client.post("/samples/s1/alerts/ack", json={"acknowledged": True}).json()["updated"]
    assert n == 2
    assert client.get("/samples/s1/state").json()["unacknowledged_alert_count"] == 0


def test_alert_dedup_survives_recompute(client) -> None:
    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2},
        ],
    )
    alert_id = client.get("/alerts?sample_id=s1").json()[0]["id"]
    client.post(f"/alerts/{alert_id}/ack", json={"acknowledged": True})

    # 新读数触发重算，告警 id 与确认状态应保留
    _submit(
        client,
        "s1",
        [{"event_id": "3", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 3}],
    )
    alerts = client.get("/alerts?sample_id=s1").json()
    assert len(alerts) == 1
    assert alerts[0]["id"] == alert_id
    assert alerts[0]["acknowledged"] is True


def test_patch_thresholds_triggers_recompute_and_keeps_ack(client) -> None:
    # 默认 -20/0：-5°C 处于滞回区，不构成循环
    _create_sample(client, max_freeze_thaw_cycles=None)
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -25},
            {"event_id": "2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -5},
            {"event_id": "3", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 4},
        ],
    )
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # 收紧解冻阈值到 -10：-5°C 即解冻，循环不变但段极值描述重算
    r = client.patch("/samples/s1", json={"thaw_threshold_c": -10})
    assert r.status_code == 200
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # 收紧冻结阈值到 -30：-25°C 不再触发冻结，循环应消失
    r = client.patch("/samples/s1", json={"freeze_threshold_c": -30})
    assert r.status_code == 200
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0

    # 非法阈值组合 422（-30 >= -10 不成立，改成解冻阈值低于冻结阈值）
    r = client.patch("/samples/s1", json={"thaw_threshold_c": -40})
    assert r.status_code == 422


def test_sample_not_found(client) -> None:
    assert client.get("/samples/nope/state").status_code == 404
    assert client.get("/alerts/999").status_code == 404


# ---------------- 累计温度暴露剂量 ----------------


def test_exposure_dose_state_and_budget_alert(client) -> None:
    _create_sample(
        client,
        min_allowed_c=-80,
        max_allowed_c=8,
        cold_dose_budget_c_min=100,
        heat_dose_budget_c_min=1000,
        max_integration_interval_minutes=120,
    )
    # -85°C 恒温 60 分钟：低温剂量 5 × 60 = 300；预算 100 在 20 分钟处跨越
    r = _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85},
        ],
    )
    assert r.status_code == 200
    new_types = [a["alert_type"] for a in r.json()["new_alerts"]]
    assert "exposure_budget_exceeded" in new_types

    state = client.get("/samples/s1/state").json()
    assert state["cold_dose_c_min"] == 300.0
    assert state["heat_dose_c_min"] == 0.0
    assert state["exposure_gap_minutes"] == 0.0
    assert state["cold_dose_budget_c_min"] == 100.0
    assert state["cold_budget_usage"] == 3.0
    assert state["heat_budget_usage"] == 0.0

    alert = next(
        a
        for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "exposure_budget_exceeded"
    )
    assert alert["direction"] == "cold"
    assert alert["severity"] == "critical"
    assert alert["event_time"].startswith("2026-09-20T08:20:00")
    assert alert["temperature_c"] == -85.0
    assert alert["budget_c_min"] == 100.0

    sample = client.get("/samples/s1").json()
    assert sample["cold_dose_budget_c_min"] == 100.0
    assert sample["cold_dose_c_min"] == 300.0


def test_exposure_gap_skips_long_intervals(client) -> None:
    _create_sample(
        client,
        max_allowed_c=8,
        max_integration_interval_minutes=60,
    )
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 20},
            # 间隔 61 分钟：跳过并计缺口
            {"event_id": "2", "event_time": "2026-09-20T09:01:00Z", "temperature_c": 20},
            # 间隔 30 分钟：正常积分（幅度 12 × 30 = 360）
            {"event_id": "3", "event_time": "2026-09-20T09:31:00Z", "temperature_c": 20},
        ],
    )
    state = client.get("/samples/s1/state").json()
    assert state["exposure_gap_minutes"] == 61.0
    assert state["heat_dose_c_min"] == 360.0


def test_exposure_budget_alert_is_deduped_and_reversible(client) -> None:
    _create_sample(client, min_allowed_c=-80, cold_dose_budget_c_min=100)
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85},
        ],
    )
    alerts_before = client.get("/alerts?sample_id=s1").json()
    budget_alerts = [a for a in alerts_before if a["alert_type"] == "exposure_budget_exceeded"]
    assert len(budget_alerts) == 1
    alert_id = budget_alerts[0]["id"]
    client.post(f"/alerts/{alert_id}/ack", json={"acknowledged": True})

    # 继续追加读数触发重算：告警仍是同一条（稳定去重），确认状态保留
    _submit(
        client,
        "s1",
        [
            {"event_id": "3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -85},
        ],
    )
    alerts_after = client.get("/alerts?sample_id=s1").json()
    budget_after = [a for a in alerts_after if a["alert_type"] == "exposure_budget_exceeded"]
    assert len(budget_after) == 1
    assert budget_after[0]["id"] == alert_id
    assert budget_after[0]["acknowledged"] is True

    # 预算调得足够大：重算后预算告警应被清除（可逆）
    r = client.patch("/samples/s1", json={"cold_dose_budget_c_min": 1_000_000})
    assert r.status_code == 200
    assert not [
        a
        for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "exposure_budget_exceeded"
    ]
    assert client.get("/samples/s1/state").json()["cold_budget_usage"] < 1

    # 再调回小预算：告警重新出现
    client.patch("/samples/s1", json={"cold_dose_budget_c_min": 100})
    assert [
        a
        for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "exposure_budget_exceeded"
    ]


def test_late_readings_recompute_exposure_dose(client) -> None:
    _create_sample(client, max_allowed_c=8, heat_dose_budget_c_min=1_000)
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 5},
            {"event_id": "3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 5},
        ],
    )
    before = client.get("/samples/s1/state").json()["heat_dose_c_min"]

    # 迟到一个 09:00 的高温点，插值剂量随之重算
    _submit(
        client,
        "s1",
        [
            {"event_id": "late", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 20},
        ],
    )
    after = client.get("/samples/s1/state").json()
    # 两段对称三角：5 -> 20 -> 5，每段在 f=0.2 处越过 8，
    # 正值段 48 分钟、幅度 0 -> 12：0.5 × 12 × 48 = 288，两段合计 576
    assert after["heat_dose_c_min"] == 576.0
    assert before != after["heat_dose_c_min"]


def test_dose_config_validation(client) -> None:
    # 低温预算但没有下限 -> 422
    r = client.post(
        "/samples",
        json={
            "id": "bad1",
            "name": "x",
            "freeze_threshold_c": -20,
            "thaw_threshold_c": 0,
            "cold_dose_budget_c_min": 100,
        },
    )
    assert r.status_code == 422

    # 预算非正 -> 422
    r = client.post(
        "/samples",
        json={
            "id": "bad2",
            "name": "x",
            "freeze_threshold_c": -20,
            "thaw_threshold_c": 0,
            "max_allowed_c": 8,
            "heat_dose_budget_c_min": 0,
        },
    )
    assert r.status_code == 422

    # PATCH 合并现有配置后仍缺边界 -> 422
    _create_sample(client, sid="s2", min_allowed_c=None, max_allowed_c=None)
    r = client.patch("/samples/s2", json={"cold_dose_budget_c_min": 100})
    assert r.status_code == 422

    # 显式 null 清空：保留冷预算时清空下限 -> 422；先清预算再清下限 -> 200
    _create_sample(client, sid="s3", cold_dose_budget_c_min=100)
    r = client.patch("/samples/s3", json={"min_allowed_c": None})
    assert r.status_code == 422
    assert client.patch("/samples/s3", json={"cold_dose_budget_c_min": None}).status_code == 200
    assert client.patch("/samples/s3", json={"min_allowed_c": None}).status_code == 200
    # 未提交 name 时的普通字段更新不受影响；显式 name=null -> 422
    assert client.patch("/samples/s3", json={"thaw_threshold_c": -5}).status_code == 200
    assert client.patch("/samples/s3", json={"name": None}).status_code == 422


def test_database_migration_from_legacy_schema(tmp_path, monkeypatch) -> None:
    """旧版本数据库（无暴露剂量列）启动时应通过 ALTER TABLE 平滑升级。"""

    import sqlite3

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE samples (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            freeze_threshold_c REAL NOT NULL, thaw_threshold_c REAL NOT NULL,
            min_allowed_c REAL, max_allowed_c REAL, max_freeze_thaw_cycles INTEGER,
            current_state TEXT NOT NULL DEFAULT 'thawed',
            active_freeze_event_time TEXT, active_freeze_min_c REAL,
            active_freeze_max_c REAL, cycle_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL,
            event_id TEXT, event_time TEXT NOT NULL, received_at TEXT NOT NULL,
            temperature_c REAL NOT NULL,
            UNIQUE(sample_id, event_id),
            UNIQUE(sample_id, event_time, temperature_c)
        );
        CREATE TABLE cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL,
            cycle_index INTEGER NOT NULL, freeze_event_time TEXT NOT NULL,
            freeze_temperature_c REAL NOT NULL, thaw_event_time TEXT NOT NULL,
            thaw_temperature_c REAL NOT NULL, min_temperature_c REAL NOT NULL,
            max_temperature_c REAL NOT NULL, UNIQUE(sample_id, cycle_index)
        );
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL,
            alert_type TEXT NOT NULL, severity TEXT NOT NULL, event_time TEXT NOT NULL,
            temperature_c REAL, cycle_index INTEGER, reading_id INTEGER,
            message TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
            dedup_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
        INSERT INTO samples VALUES (
            'legacy-1', '旧库样本', -20, 0, -80, 8, 2,
            'thawed', NULL, NULL, NULL, 0, '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(legacy))

    import app.database as database
    import app.services as services
    import app.main as main

    importlib.reload(database)
    importlib.reload(services)
    importlib.reload(main)
    database.init_db()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        # 旧样本仍可查询，新列有默认值
        state = c.get("/samples/legacy-1/state").json()
        assert state["cold_dose_c_min"] == 0.0
        assert state["heat_dose_c_min"] == 0.0
        assert state["exposure_gap_minutes"] == 0.0
        assert state["cold_budget_usage"] is None

        # 旧样本可 PATCH 预算并正常重算
        r = c.patch("/samples/legacy-1", json={"cold_dose_budget_c_min": 100})
        assert r.status_code == 200
        r = c.post(
            "/samples/legacy-1/readings",
            json={
                "readings": [
                    {"event_id": "x1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
                    {"event_id": "x2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85},
                ]
            },
        )
        assert r.status_code == 200
        state = c.get("/samples/legacy-1/state").json()
        assert state["cold_dose_c_min"] == 300.0
        assert any(
            a["alert_type"] == "exposure_budget_exceeded"
            for a in c.get("/alerts?sample_id=legacy-1").json()
        )
