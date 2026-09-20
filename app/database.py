"""SQLite 连接管理与建表初始化。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 本地开发默认 data/freeze_thaw.db；容器内通过环境变量指向 /data/freeze_thaw.db
DB_PATH = os.environ.get("DB_PATH", "data/freeze_thaw.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS samples (
    id                        TEXT PRIMARY KEY,
    name                      TEXT NOT NULL,
    freeze_threshold_c        REAL NOT NULL,
    thaw_threshold_c          REAL NOT NULL,
    min_allowed_c             REAL,
    max_allowed_c             REAL,
    max_freeze_thaw_cycles    INTEGER,
    low_exposure_budget_c_min REAL,
    high_exposure_budget_c_min REAL,
    max_integration_gap_minutes REAL,
    -- 以下为每次重算后刷新的派生缓存，便于直接查询当前状态
    current_state             TEXT NOT NULL DEFAULT 'thawed',
    active_freeze_event_time  TEXT,
    active_freeze_min_c       REAL,
    active_freeze_max_c       REAL,
    cycle_count               INTEGER NOT NULL DEFAULT 0,
    low_exposure_c_min        REAL NOT NULL DEFAULT 0,
    high_exposure_c_min       REAL NOT NULL DEFAULT 0,
    exposure_gap_minutes      REAL NOT NULL DEFAULT 0,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id      TEXT NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    event_id       TEXT,
    event_time     TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    temperature_c  REAL NOT NULL,
    -- 幂等去重：客户端事件 ID 唯一；无事件 ID 时，同一时刻同一温度视为重复
    UNIQUE(sample_id, event_id),
    UNIQUE(sample_id, event_time, temperature_c)
);

CREATE INDEX IF NOT EXISTS idx_readings_sample_time
    ON readings(sample_id, event_time, id);

CREATE TABLE IF NOT EXISTS cycles (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id             TEXT NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    cycle_index           INTEGER NOT NULL,
    freeze_event_time     TEXT NOT NULL,
    freeze_temperature_c  REAL NOT NULL,
    thaw_event_time       TEXT NOT NULL,
    thaw_temperature_c    REAL NOT NULL,
    min_temperature_c     REAL NOT NULL,
    max_temperature_c     REAL NOT NULL,
    UNIQUE(sample_id, cycle_index)
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id      TEXT NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    alert_type     TEXT NOT NULL,
    severity       TEXT NOT NULL,
    event_time     TEXT NOT NULL,
    temperature_c  REAL,
    cycle_index    INTEGER,
    reading_id     INTEGER REFERENCES readings(id) ON DELETE CASCADE,
    exposure_side  TEXT,
    exposure_c_min REAL,
    exposure_budget_c_min REAL,
    message        TEXT NOT NULL,
    acknowledged   INTEGER NOT NULL DEFAULT 0,
    dedup_key      TEXT NOT NULL UNIQUE,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_sample_time
    ON alerts(sample_id, event_time, id);

CREATE INDEX IF NOT EXISTS idx_alerts_unack
    ON alerts(acknowledged, event_time);
"""

# 旧版本数据库的兼容迁移：对已存在的表幂等补齐新增列。
# 全新建库时这些列已在 SCHEMA 中，ALTER 会自动跳过。
_MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "samples": [
        ("low_exposure_budget_c_min", "REAL"),
        ("high_exposure_budget_c_min", "REAL"),
        ("max_integration_gap_minutes", "REAL"),
        ("low_exposure_c_min", "REAL NOT NULL DEFAULT 0"),
        ("high_exposure_c_min", "REAL NOT NULL DEFAULT 0"),
        ("exposure_gap_minutes", "REAL NOT NULL DEFAULT 0"),
    ],
    "alerts": [
        ("exposure_side", "TEXT"),
        ("exposure_c_min", "REAL"),
        ("exposure_budget_c_min", "REAL"),
    ],
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """对老版本数据库幂等 ALTER TABLE 增补列，不破坏既有数据。"""

    for table, columns in _MIGRATION_COLUMNS.items():
        # 全新部署时表可能尚不存在（由 SCHEMA 创建），跳过即可
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            continue
        present = _table_columns(conn, table)
        for name, decl in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    """返回普通 dict：业务代码可用 row["x"]，Pydantic from_attributes 也能序列化。"""

    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个 SQLite 连接（行工厂为 dict，外键约束开启）。"""

    conn = sqlite3.connect(db_path or DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    """创建数据目录并执行幂等建表脚本与兼容迁移，启动时调用。"""

    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()
