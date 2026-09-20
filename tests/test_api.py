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
