"""请求 / 响应 Pydantic 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


def _freeze_lt_thaw(data: dict) -> dict:
    freeze = data.get("freeze_threshold_c")
    thaw = data.get("thaw_threshold_c")
    if freeze is not None and thaw is not None and freeze >= thaw:
        raise ValueError("freeze_threshold_c 必须严格小于 thaw_threshold_c")
    return data


# ---------------- 请求模型 ----------------


class SampleCreate(BaseModel):
    id: Optional[str] = Field(None, max_length=128, description="不填则自动生成 UUID")
    name: str = Field(..., max_length=256)
    freeze_threshold_c: float = Field(..., description="温度 <= 该值判定为冻结（默认建议 -20）")
    thaw_threshold_c: float = Field(..., description="温度 >= 该值判定为解冻（默认建议 0）")
    min_allowed_c: Optional[float] = Field(None, description="允许的最低温度，低于即告警")
    max_allowed_c: Optional[float] = Field(None, description="允许的最高温度，高于即告警")
    max_freeze_thaw_cycles: Optional[int] = Field(None, ge=0, description="允许的最大冻融循环次数")
    low_exposure_budget_c_min: Optional[float] = Field(
        None,
        gt=0,
        description="超低温（低于 min_allowed_c）累计暴露剂量预算，单位 °C·分钟",
    )
    high_exposure_budget_c_min: Optional[float] = Field(
        None,
        gt=0,
        description="超高温（高于 max_allowed_c）累计暴露剂量预算，单位 °C·分钟",
    )
    max_integration_gap_minutes: Optional[float] = Field(
        None,
        gt=0,
        description="相邻读数可积分的最大间隔（分钟）；超过则跳过该段并计入缺口",
    )

    @model_validator(mode="after")
    def _validate_create(self):
        if self.freeze_threshold_c >= self.thaw_threshold_c:
            raise ValueError("freeze_threshold_c 必须严格小于 thaw_threshold_c")
        return self


class SampleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=256)
    freeze_threshold_c: Optional[float] = None
    thaw_threshold_c: Optional[float] = None
    min_allowed_c: Optional[float] = None
    max_allowed_c: Optional[float] = None
    max_freeze_thaw_cycles: Optional[int] = Field(None, ge=0)
    low_exposure_budget_c_min: Optional[float] = Field(None, gt=0)
    high_exposure_budget_c_min: Optional[float] = Field(None, gt=0)
    max_integration_gap_minutes: Optional[float] = Field(None, gt=0)

    @model_validator(mode="after")
    def _validate(self):
        _freeze_lt_thaw(self.model_dump(exclude_unset=True))
        return self


class ReadingIn(BaseModel):
    temperature_c: float = Field(..., description="摄氏温度读数")
    event_time: datetime = Field(..., description="读数发生时间（ISO 8601，无时区按 UTC 处理）")
    event_id: Optional[str] = Field(
        None,
        max_length=128,
        description="客户端幂等键；不填则以 (event_time, temperature_c) 去重",
    )


class ReadingsBatch(BaseModel):
    readings: list[ReadingIn] = Field(..., min_length=1, max_length=10_000)


class AckRequest(BaseModel):
    acknowledged: bool = True


# ---------------- 响应模型 ----------------


class SampleOut(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    freeze_threshold_c: float
    thaw_threshold_c: float
    min_allowed_c: Optional[float]
    max_allowed_c: Optional[float]
    max_freeze_thaw_cycles: Optional[int]
    low_exposure_budget_c_min: Optional[float]
    high_exposure_budget_c_min: Optional[float]
    max_integration_gap_minutes: Optional[float]
    current_state: str
    active_freeze_event_time: Optional[datetime]
    active_freeze_min_c: Optional[float]
    active_freeze_max_c: Optional[float]
    cycle_count: int
    low_exposure_c_min: float
    high_exposure_c_min: float
    exposure_gap_minutes: float
    created_at: datetime
    updated_at: datetime


class ReadingOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    sample_id: str
    event_id: Optional[str]
    event_time: datetime
    received_at: datetime
    temperature_c: float


class CycleOut(BaseModel):
    model_config = {"from_attributes": True}

    cycle_index: int
    freeze_event_time: datetime
    freeze_temperature_c: float
    thaw_event_time: datetime
    thaw_temperature_c: float
    min_temperature_c: float
    max_temperature_c: float


class AlertOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    sample_id: str
    alert_type: str
    severity: str
    event_time: datetime
    temperature_c: Optional[float]
    cycle_index: Optional[int]
    reading_id: Optional[int]
    exposure_side: Optional[str]
    exposure_c_min: Optional[float]
    exposure_budget_c_min: Optional[float]
    message: str
    acknowledged: bool
    created_at: datetime


class SampleStateOut(BaseModel):
    sample_id: str
    name: str
    current_state: str
    cycle_count: int
    active_freeze_event_time: Optional[datetime]
    active_freeze_min_c: Optional[float]
    active_freeze_max_c: Optional[float]
    last_event_time: Optional[datetime]
    last_temperature_c: Optional[float]
    # 双向累计暴露剂量（°C·分钟）与因间隔超限未积分的缺口时长（分钟）
    low_exposure_c_min: float
    high_exposure_c_min: float
    exposure_gap_minutes: float
    low_exposure_budget_c_min: Optional[float]
    high_exposure_budget_c_min: Optional[float]
    # 预算使用率（累计剂量 / 预算）；未配置预算侧为 None
    low_exposure_budget_used_ratio: Optional[float]
    high_exposure_budget_used_ratio: Optional[float]
    unacknowledged_alert_count: int


class BatchReadingsResponse(BaseModel):
    sample_id: str
    accepted: int
    duplicates: int
    current_state: str
    cycle_count: int
    new_alerts: list[AlertOut]


class RecomputeResponse(BaseModel):
    sample_id: str
    cycle_count: int
    current_state: str
    alert_count: int
    new_alert_count: int
