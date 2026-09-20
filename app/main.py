"""FastAPI 入口：启动即建库，并暴露样本 / 读数 / 状态 / 循环 / 告警接口。"""

from __future__ import annotations

from typing import Optional

import sqlite3
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from . import __version__, services
from .database import init_db
from .schemas import (
    AckRequest,
    AlertOut,
    BatchReadingsResponse,
    CycleOut,
    ReadingOut,
    RecomputeResponse,
    ReadingIn,
    ReadingsBatch,
    SampleCreate,
    SampleOut,
    SampleStateOut,
    SampleUpdate,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动即初始化数据库（幂等建表、索引）
    init_db()
    yield


app = FastAPI(
    title="实验室样本冻融追踪 API",
    description=(
        "接收带事件时间的样本温度读数，按时间顺序重排迟到数据，"
        "以可配置的冻结/解冻阈值和滞回规则识别完整冻融循环，"
        "并按线性插值累计超低温/超高温暴露剂量（°C·分钟）与缺口时长，"
        "提供幂等去重、当前状态、循环次数、预算使用率与超限告警查询。"
    ),
    version=__version__,
    lifespan=lifespan,
)


def _require_sample(conn: sqlite3.Connection, sample_id: str) -> sqlite3.Row:
    sample = services.get_sample(conn, sample_id)
    if sample is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="样本不存在")
    return sample


# ---------------- 健康检查 / 文档 ----------------


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


# ---------------- 样本管理 ----------------


@app.post("/samples", response_model=SampleOut, status_code=201, tags=["samples"])
def create_sample(payload: SampleCreate, conn=Depends(services.get_db_connection)):
    data = payload.model_dump()
    if data.get("id"):
        if services.get_sample(conn, data["id"]) is not None:
            raise HTTPException(status_code=409, detail="样本 ID 已存在")
    return services.create_sample(conn, data)


@app.get("/samples", response_model=list[SampleOut], tags=["samples"])
def list_samples(conn=Depends(services.get_db_connection)):
    return services.list_samples(conn)


@app.get("/samples/{sample_id}", response_model=SampleOut, tags=["samples"])
def get_sample(sample_id: str, conn=Depends(services.get_db_connection)):
    return _require_sample(conn, sample_id)


@app.patch("/samples/{sample_id}", response_model=SampleOut, tags=["samples"])
def update_sample(
    sample_id: str, payload: SampleUpdate, conn=Depends(services.get_db_connection)
):
    _require_sample(conn, sample_id)
    changes = payload.model_dump(exclude_unset=True)
    existing = services.get_sample(conn, sample_id)
    merged = {
        "freeze_threshold_c": existing["freeze_threshold_c"],
        "thaw_threshold_c": existing["thaw_threshold_c"],
        **{k: v for k, v in changes.items() if v is not None},
    }
    if merged["freeze_threshold_c"] >= merged["thaw_threshold_c"]:
        raise HTTPException(
            status_code=422,
            detail="freeze_threshold_c 必须严格小于 thaw_threshold_c",
        )
    services.update_sample(conn, sample_id, changes)
    # 阈值/区间/循环上限/暴露预算变化后按新配置重算（告警确认状态通过 dedup_key 保留）
    if any(
        k in changes
        for k in (
            "freeze_threshold_c",
            "thaw_threshold_c",
            "min_allowed_c",
            "max_allowed_c",
            "max_freeze_thaw_cycles",
            "low_exposure_budget_c_min",
            "high_exposure_budget_c_min",
            "max_integration_gap_minutes",
        )
    ):
        services.recompute(conn, sample_id)
    return services.get_sample(conn, sample_id)


@app.delete("/samples/{sample_id}", status_code=204, tags=["samples"])
def delete_sample(sample_id: str, conn=Depends(services.get_db_connection)):
    if not services.delete_sample(conn, sample_id):
        raise HTTPException(status_code=404, detail="样本不存在")


# ---------------- 读数提交（幂等） ----------------


@app.post(
    "/samples/{sample_id}/readings",
    response_model=BatchReadingsResponse,
    status_code=200,
    tags=["readings"],
)
def submit_readings(
    sample_id: str,
    payload: ReadingsBatch,
    conn=Depends(services.get_db_connection),
):
    _require_sample(conn, sample_id)
    accepted, duplicates, new_alerts = services.add_readings(
        conn,
        sample_id,
        [r.model_dump() for r in payload.readings],
    )
    sample = services.get_sample(conn, sample_id)
    return BatchReadingsResponse(
        sample_id=sample_id,
        accepted=accepted,
        duplicates=duplicates,
        current_state=sample["current_state"],
        cycle_count=sample["cycle_count"],
        new_alerts=new_alerts,
    )


@app.get(
    "/samples/{sample_id}/readings",
    response_model=list[ReadingOut],
    tags=["readings"],
)
def get_readings(sample_id: str, conn=Depends(services.get_db_connection)):
    _require_sample(conn, sample_id)
    return services.list_readings(conn, sample_id)


# ---------------- 状态与循环 ----------------


@app.get(
    "/samples/{sample_id}/state",
    response_model=SampleStateOut,
    tags=["state"],
)
def get_sample_state(sample_id: str, conn=Depends(services.get_db_connection)):
    _require_sample(conn, sample_id)
    return services.get_state(conn, sample_id)


@app.get(
    "/samples/{sample_id}/cycles",
    response_model=list[CycleOut],
    tags=["state"],
)
def get_cycles(sample_id: str, conn=Depends(services.get_db_connection)):
    _require_sample(conn, sample_id)
    return services.list_cycles(conn, sample_id)


@app.post(
    "/samples/{sample_id}/recompute",
    response_model=RecomputeResponse,
    tags=["state"],
)
def recompute_sample(sample_id: str, conn=Depends(services.get_db_connection)):
    """手动触发重算（正常提交读数后会自动重算，此接口用于兜底/运维）。"""

    _require_sample(conn, sample_id)
    before = {
        row["dedup_key"]
        for row in conn.execute(
            "SELECT dedup_key FROM alerts WHERE sample_id = ?", (sample_id,)
        )
    }
    new_rows = services.recompute(conn, sample_id)
    sample = services.get_sample(conn, sample_id)
    alert_count = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE sample_id = ?", (sample_id,)
    ).fetchone()["n"]
    new_count = sum(1 for row in new_rows if row["dedup_key"] not in before)
    return RecomputeResponse(
        sample_id=sample_id,
        cycle_count=sample["cycle_count"],
        current_state=sample["current_state"],
        alert_count=alert_count,
        new_alert_count=new_count,
    )


# ---------------- 告警 ----------------


@app.get("/alerts", response_model=list[AlertOut], tags=["alerts"])
def query_alerts(
    sample_id: Optional[str] = None,
    acknowledged: Optional[bool] = Query(None, description="true/false 过滤确认状态"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    conn=Depends(services.get_db_connection),
):
    return services.list_alerts(
        conn,
        sample_id=sample_id,
        acknowledged=acknowledged,
        limit=limit,
        offset=offset,
    )


@app.get("/alerts/{alert_id}", response_model=AlertOut, tags=["alerts"])
def get_alert(alert_id: int, conn=Depends(services.get_db_connection)):
    alert = services.get_alert(conn, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return alert


@app.post("/alerts/{alert_id}/ack", response_model=AlertOut, tags=["alerts"])
def acknowledge_alert(
    alert_id: int,
    payload: AckRequest,
    conn=Depends(services.get_db_connection),
):
    alert = services.acknowledge_alert(conn, alert_id, payload.acknowledged)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return alert


@app.post("/samples/{sample_id}/alerts/ack", tags=["alerts"])
def acknowledge_sample_alerts(
    sample_id: str,
    payload: AckRequest,
    conn=Depends(services.get_db_connection),
):
    _require_sample(conn, sample_id)
    count = services.acknowledge_sample_alerts(conn, sample_id, payload.acknowledged)
    return {"sample_id": sample_id, "updated": count, "acknowledged": payload.acknowledged}
