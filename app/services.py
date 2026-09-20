"""业务服务层：读数写入、时间重排重算、循环/告警查询与确认。"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import engine
from .database import connect

# SQLite 单文件写入需要串行化；批量提交 + 重算整体持锁。
_write_lock = threading.RLock()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_utc_iso(dt: datetime) -> str:
    """归一化为 UTC ISO 8601（无时区的输入按 UTC 处理），保证字符串排序即时间排序。"""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------- 样本 ----------------


def create_sample(conn: sqlite3.Connection, data: dict) -> sqlite3.Row:
    now = utcnow_iso()
    sample_id = data.get("id") or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO samples (
            id, name, freeze_threshold_c, thaw_threshold_c,
            min_allowed_c, max_allowed_c, max_freeze_thaw_cycles,
            cold_dose_budget_c_min, heat_dose_budget_c_min,
            max_integration_interval_minutes,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sample_id,
            data["name"],
            data["freeze_threshold_c"],
            data["thaw_threshold_c"],
            data.get("min_allowed_c"),
            data.get("max_allowed_c"),
            data.get("max_freeze_thaw_cycles"),
            data.get("cold_dose_budget_c_min"),
            data.get("heat_dose_budget_c_min"),
            data.get("max_integration_interval_minutes"),
            now,
            now,
        ),
    )
    conn.commit()
    row = get_sample(conn, sample_id)
    assert row is not None
    return row


def get_sample(conn: sqlite3.Connection, sample_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()


def list_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM samples ORDER BY created_at, id"))


def update_sample(conn: sqlite3.Connection, sample_id: str, changes: dict) -> Optional[sqlite3.Row]:
    # changes 来自 exclude_unset：只含客户端显式提交的字段，
    # 因此显式传入的 null 也要落库（用于清空允许边界 / 预算等可空配置）。
    if "name" in changes and changes["name"] is None:
        raise ValueError("name 不能为空")
    if changes:
        assignments = ", ".join(f"{k} = ?" for k in changes)
        params = [*changes.values(), utcnow_iso(), sample_id]
        conn.execute(
            f"UPDATE samples SET {assignments}, updated_at = ? WHERE id = ?",
            params,
        )
        conn.commit()
    return get_sample(conn, sample_id)


def delete_sample(conn: sqlite3.Connection, sample_id: str) -> bool:
    cur = conn.execute("DELETE FROM samples WHERE id = ?", (sample_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------- 读数 ----------------


def list_readings(conn: sqlite3.Connection, sample_id: str) -> list[sqlite3.Row]:
    # 时间顺序：迟到数据也按 event_time 排列，而非入库顺序
    return list(
        conn.execute(
            "SELECT * FROM readings WHERE sample_id = ? ORDER BY event_time, id",
            (sample_id,),
        )
    )


def add_readings(
    conn: sqlite3.Connection, sample_id: str, readings: list[dict]
) -> tuple[int, int, list[sqlite3.Row]]:
    """幂等批量写入读数，随后重算样本派生数据。

    返回 ``(accepted, duplicates, new_alerts)``。
    同一批次内或库中已存在相同 event_id、或相同 (event_time, temperature_c)
    的读数均视为重复并跳过。
    """

    now = utcnow_iso()
    seen_event_ids: set[str] = set()
    seen_signatures: set[tuple[str, float]] = set()
    accepted = 0
    duplicates = 0

    with _write_lock:
        for r in readings:
            event_time_iso = to_utc_iso(r["event_time"])
            event_id = r.get("event_id")
            temp = float(r["temperature_c"])
            is_dup = False

            if event_id is not None:
                if event_id in seen_event_ids:
                    is_dup = True
                else:
                    row = conn.execute(
                        "SELECT 1 FROM readings WHERE sample_id = ? AND event_id = ?",
                        (sample_id, event_id),
                    ).fetchone()
                    if row is not None:
                        is_dup = True
                    else:
                        seen_event_ids.add(event_id)

            signature = (event_time_iso, temp)
            if not is_dup:
                if signature in seen_signatures:
                    is_dup = True
                else:
                    row = conn.execute(
                        "SELECT 1 FROM readings WHERE sample_id = ? AND event_time = ? AND temperature_c = ?",
                        (sample_id, event_time_iso, temp),
                    ).fetchone()
                    if row is not None:
                        is_dup = True
                    else:
                        seen_signatures.add(signature)

            if is_dup:
                duplicates += 1
                continue

            conn.execute(
                """
                INSERT INTO readings (sample_id, event_id, event_time, received_at, temperature_c)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sample_id, event_id, event_time_iso, now, temp),
            )
            accepted += 1

        conn.commit()
        new_alerts = recompute(conn, sample_id, lock_held=True)

    return accepted, duplicates, new_alerts


# ---------------- 重算（核心） ----------------


def _sample_config(sample: sqlite3.Row) -> engine.EngineConfig:
    return engine.EngineConfig(
        freeze_threshold_c=sample["freeze_threshold_c"],
        thaw_threshold_c=sample["thaw_threshold_c"],
        min_allowed_c=sample["min_allowed_c"],
        max_allowed_c=sample["max_allowed_c"],
        max_freeze_thaw_cycles=sample["max_freeze_thaw_cycles"],
        cold_dose_budget_c_min=sample["cold_dose_budget_c_min"],
        heat_dose_budget_c_min=sample["heat_dose_budget_c_min"],
        max_integration_interval_minutes=sample["max_integration_interval_minutes"],
    )


def recompute(
    conn: sqlite3.Connection,
    sample_id: str,
    lock_held: bool = False,
) -> list[sqlite3.Row]:
    """按时间顺序重放全部读数，重建循环与告警，并刷新样本状态缓存。

    告警以稳定 dedup_key 做 upsert，历史告警的确认状态不会丢失；
    本次新产生的告警行会作为列表返回。已不存在的陈旧告警会被清除。
    返回值即“新告警”，供提交接口即时反馈。
    """

    def _do() -> list[sqlite3.Row]:
        sample = get_sample(conn, sample_id)
        if sample is None:
            raise KeyError(sample_id)

        rows = list_readings(conn, sample_id)
        eng_readings = [
            engine.EngineReading(
                id=row["id"],
                event_time=row["event_time"],
                temperature_c=row["temperature_c"],
            )
            for row in rows
        ]
        derived = engine.derive(eng_readings, _sample_config(sample))
        now = utcnow_iso()

        # 1) 重建完整循环
        conn.execute("DELETE FROM cycles WHERE sample_id = ?", (sample_id,))
        conn.executemany(
            """
            INSERT INTO cycles (
                sample_id, cycle_index, freeze_event_time, freeze_temperature_c,
                thaw_event_time, thaw_temperature_c, min_temperature_c, max_temperature_c
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    sample_id,
                    c.cycle_index,
                    c.freeze_event_time,
                    c.freeze_temperature_c,
                    c.thaw_event_time,
                    c.thaw_temperature_c,
                    c.min_temperature_c,
                    c.max_temperature_c,
                )
                for c in derived.cycles
            ],
        )

        # 2) 告警 upsert：按 dedup_key 保留已确认标记
        current_keys = {engine.alert_dedup_key(sample_id, a) for a in derived.alerts}
        existing = {
            row["dedup_key"]: row
            for row in conn.execute(
                "SELECT * FROM alerts WHERE sample_id = ?", (sample_id,)
            )
        }
        # 清理：重算后已不再成立的告警
        stale_keys = set(existing) - current_keys
        if stale_keys:
            conn.executemany(
                "DELETE FROM alerts WHERE dedup_key = ?",
                [(k,) for k in stale_keys],
            )

        new_keys: list[str] = []
        for a in derived.alerts:
            key = engine.alert_dedup_key(sample_id, a)
            if key in existing:
                # 内容可能随配置变化，更新描述但保留 acknowledged
                conn.execute(
                    """
                    UPDATE alerts SET
                        alert_type = ?, severity = ?, event_time = ?,
                        temperature_c = ?, cycle_index = ?, reading_id = ?,
                        direction = ?, dose_c_min = ?, budget_c_min = ?, message = ?
                    WHERE dedup_key = ?
                    """,
                    (
                        a.alert_type,
                        a.severity,
                        a.event_time,
                        a.temperature_c,
                        a.cycle_index,
                        a.reading_id,
                        a.direction,
                        a.dose_c_min,
                        a.budget_c_min,
                        a.message,
                        key,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO alerts (
                        sample_id, alert_type, severity, event_time, temperature_c,
                        cycle_index, reading_id, direction, dose_c_min, budget_c_min,
                        message, acknowledged, dedup_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        sample_id,
                        a.alert_type,
                        a.severity,
                        a.event_time,
                        a.temperature_c,
                        a.cycle_index,
                        a.reading_id,
                        a.direction,
                        a.dose_c_min,
                        a.budget_c_min,
                        a.message,
                        key,
                        now,
                    ),
                )
                new_keys.append(key)

        # 3) 刷新样本当前状态缓存
        conn.execute(
            """
            UPDATE samples SET
                current_state = ?,
                active_freeze_event_time = ?,
                active_freeze_min_c = ?,
                active_freeze_max_c = ?,
                cycle_count = ?,
                cold_dose_c_min = ?,
                heat_dose_c_min = ?,
                exposure_gap_minutes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                derived.current_state,
                derived.active_freeze_event_time,
                derived.active_freeze_min_c,
                derived.active_freeze_max_c,
                derived.cycle_count,
                derived.cold_dose_c_min,
                derived.heat_dose_c_min,
                derived.gap_minutes,
                now,
                sample_id,
            ),
        )
        conn.commit()

        if new_keys:
            placeholders = ",".join("?" for _ in new_keys)
            return list(
                conn.execute(
                    f"SELECT * FROM alerts WHERE dedup_key IN ({placeholders}) ORDER BY event_time, id",
                    new_keys,
                )
            )
        return []

    if lock_held:
        return _do()
    with _write_lock:
        return _do()


# ---------------- 查询 ----------------


def list_cycles(conn: sqlite3.Connection, sample_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM cycles WHERE sample_id = ? ORDER BY cycle_index",
            (sample_id,),
        )
    )


def list_alerts(
    conn: sqlite3.Connection,
    sample_id: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM alerts WHERE 1=1"
    params: list = []
    if sample_id is not None:
        sql += " AND sample_id = ?"
        params.append(sample_id)
    if acknowledged is not None:
        sql += " AND acknowledged = ?"
        params.append(1 if acknowledged else 0)
    sql += " ORDER BY event_time DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return list(conn.execute(sql, params))


def get_alert(conn: sqlite3.Connection, alert_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


def acknowledge_alert(
    conn: sqlite3.Connection, alert_id: int, acknowledged: bool
) -> Optional[sqlite3.Row]:
    with _write_lock:
        cur = conn.execute(
            "UPDATE alerts SET acknowledged = ? WHERE id = ?",
            (1 if acknowledged else 0, alert_id),
        )
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_alert(conn, alert_id)


def acknowledge_sample_alerts(
    conn: sqlite3.Connection, sample_id: str, acknowledged: bool = True
) -> int:
    with _write_lock:
        cur = conn.execute(
            "UPDATE alerts SET acknowledged = ? WHERE sample_id = ?",
            (1 if acknowledged else 0, sample_id),
        )
        conn.commit()
        return cur.rowcount


def get_state(conn: sqlite3.Connection, sample_id: str) -> Optional[dict]:
    sample = get_sample(conn, sample_id)
    if sample is None:
        return None
    last = conn.execute(
        "SELECT event_time, temperature_c FROM readings WHERE sample_id = ? "
        "ORDER BY event_time DESC, id DESC LIMIT 1",
        (sample_id,),
    ).fetchone()
    unacked = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE sample_id = ? AND acknowledged = 0",
        (sample_id,),
    ).fetchone()["n"]

    def _usage(dose: float, budget: Optional[float]) -> Optional[float]:
        if budget is None or budget <= 0:
            return None
        return round(dose / budget, 6)

    return {
        "sample_id": sample["id"],
        "name": sample["name"],
        "current_state": sample["current_state"],
        "cycle_count": sample["cycle_count"],
        "active_freeze_event_time": sample["active_freeze_event_time"],
        "active_freeze_min_c": sample["active_freeze_min_c"],
        "active_freeze_max_c": sample["active_freeze_max_c"],
        "cold_dose_c_min": sample["cold_dose_c_min"],
        "heat_dose_c_min": sample["heat_dose_c_min"],
        "exposure_gap_minutes": sample["exposure_gap_minutes"],
        "cold_dose_budget_c_min": sample["cold_dose_budget_c_min"],
        "heat_dose_budget_c_min": sample["heat_dose_budget_c_min"],
        "max_integration_interval_minutes": sample["max_integration_interval_minutes"],
        "cold_budget_usage": _usage(
            sample["cold_dose_c_min"], sample["cold_dose_budget_c_min"]
        ),
        "heat_budget_usage": _usage(
            sample["heat_dose_c_min"], sample["heat_dose_budget_c_min"]
        ),
        "last_event_time": last["event_time"] if last else None,
        "last_temperature_c": last["temperature_c"] if last else None,
        "unacknowledged_alert_count": unacked,
    }


def get_db_connection() -> sqlite3.Connection:
    """FastAPI 依赖：每请求一个连接。"""

    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
