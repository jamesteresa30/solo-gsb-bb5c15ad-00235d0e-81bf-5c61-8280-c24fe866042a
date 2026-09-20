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
    id                              TEXT PRIMARY KEY,
    name                            TEXT NOT NULL,
    freeze_threshold_c              REAL NOT NULL,
    thaw_threshold_c                REAL NOT NULL,
    min_allowed_c                   REAL,
    max_allowed_c                   REAL,
    max_freeze_thaw_cycles          INTEGER,
    -- 低温/高温累计暴露剂量预算（°C·min）与最大可积分间隔（分钟）
    cold_dose_budget_c_min          REAL,
    heat_dose_budget_c_min          REAL,
    max_integration_interval_minutes REAL,
    -- 以下为每次重算后刷新的派生缓存，便于直接查询当前状态
    current_state             TEXT NOT NULL DEFAULT 'thawed',
    active_freeze_event_time  TEXT,
    active_freeze_min_c       REAL,
    active_freeze_max_c       REAL,
    cycle_count               INTEGER NOT NULL DEFAULT 0,
    -- 暴露剂量缓存：累计低温/高温剂量（°C·min）与缺口总时长（分钟）
    cold_dose_c_min           REAL NOT NULL DEFAULT 0,
    heat_dose_c_min           REAL NOT NULL DEFAULT 0,
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
    -- exposure_budget_exceeded 告警的方向（cold/heat）与跨越时剂量/预算
    direction      TEXT,
    dose_c_min     REAL,
    budget_c_min   REAL,
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


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    """返回普通 dict：业务代码可用 row["x"]，Pydantic from_attributes 也能序列化。"""

    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个 SQLite 连接（行工厂为 dict，外键约束开启）。"""

    conn = sqlite3.connect(db_path or DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 旧版本数据库的增量列：CREATE TABLE IF NOT EXISTS 不会为已存在的表补列，
# 因此启动时检测缺失列并以 ALTER TABLE 平滑加列（SQLite >= 3.35 / 各旧版本均支持）。
_MIGRATION_COLUMNS = {
    "samples": {
        "cold_dose_budget_c_min": "REAL",
        "heat_dose_budget_c_min": "REAL",
        "max_integration_interval_minutes": "REAL",
        "cold_dose_c_min": "REAL NOT NULL DEFAULT 0",
        "heat_dose_c_min": "REAL NOT NULL DEFAULT 0",
        "exposure_gap_minutes": "REAL NOT NULL DEFAULT 0",
    },
    "alerts": {
        "direction": "TEXT",
        "dose_c_min": "REAL",
        "budget_c_min": "REAL",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧库补齐新版本引入的列；新库已是完整 schema，所有检查均为空操作。"""

    for table, columns in _MIGRATION_COLUMNS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if not existing:
            # 表尚不存在：SCHEMA 中的 CREATE TABLE 会直接建出完整结构
            continue
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db(db_path: str | None = None) -> None:
    """创建数据目录并执行幂等建表脚本，启动时调用；旧库自动兼容迁移。"""

    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
