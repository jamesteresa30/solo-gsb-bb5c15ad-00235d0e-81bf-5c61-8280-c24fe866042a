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


def _validate_dose_fields(data: dict) -> dict:
    """暴露剂量相关字段的跨字段校验。"""

    for name in (
        "cold_dose_budget_c_min",
        "heat_dose_budget_c_min",
        "max_integration_interval_minutes",
    ):
        value = data.get(name)
        if value is not None and value <= 0:
            raise ValueError(f"{name} 必须为正数")

    if data.get("cold_dose_budget_c_min") is not None and data.get("min_allowed_c") is None:
        raise ValueError("配置 cold_dose_budget_c_min 时必须同时提供 min_allowed_c")
    if data.get("heat_dose_budget_c_min") is not None and data.get("max_allowed_c") is None:
        raise ValueError("配置 heat_dose_budget_c_min 时必须同时提供 max_allowed_c")
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
    cold_dose_budget_c_min: Optional[float] = Field(
        None,
        gt=0,
        description="低温累计暴露剂量预算（°C·min），首次跨越产生 exposure_budget_exceeded 告警",
    )
    heat_dose_budget_c_min: Optional[float] = Field(
        None, gt=0, description="高温累计暴露剂量预算（°C·min）"
    )
    max_integration_interval_minutes: Optional[float] = Field(
        None,
        gt=0,
        description="相邻读数最大可积分间隔（分钟）；超过则跳过该区间并计入缺口时长",
    )

    @model_validator(mode="after")
    def _validate(self):
        if self.freeze_threshold_c >= self.thaw_threshold_c:
            raise ValueError("freeze_threshold_c 必须严格小于 thaw_threshold_c")
        _validate_dose_fields(self.model_dump())
        return self


class SampleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=256)
    freeze_threshold_c: Optional[float] = None
    thaw_threshold_c: Optional[float] = None
    min_allowed_c: Optional[float] = None
    max_allowed_c: Optional[float] = None
    max_freeze_thaw_cycles: Optional[int] = Field(None, ge=0)
    cold_dose_budget_c_min: Optional[float] = Field(None, gt=0)
    heat_dose_budget_c_min: Optional[float] = Field(None, gt=0)
    max_integration_interval_minutes: Optional[float] = Field(None, gt=0)

    @model_validator(mode="after")
    def _validate(self):
        data = self.model_dump(exclude_unset=True)
        _freeze_lt_thaw(data)
        # 是否同时提供允许区间边界的跨字段检查由路由层合并现有配置后完成
        for name in (
            "cold_dose_budget_c_min",
            "heat_dose_budget_c_min",
            "max_integration_interval_minutes",
        ):
            if data.get(name) is not None and data[name] <= 0:
                raise ValueError(f"{name} 必须为正数")
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
    cold_dose_budget_c_min: Optional[float]
    heat_dose_budget_c_min: Optional[float]
    max_integration_interval_minutes: Optional[float]
    current_state: str
    active_freeze_event_time: Optional[datetime]
    active_freeze_min_c: Optional[float]
    active_freeze_max_c: Optional[float]
    cycle_count: int
    cold_dose_c_min: float
    heat_dose_c_min: float
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
    direction: Optional[str]
    dose_c_min: Optional[float]
    budget_c_min: Optional[float]
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
    # 双向累计暴露剂量（°C·min）、缺口总时长（分钟）与预算使用率（0~∞，>=1 即超限）
    cold_dose_c_min: float
    heat_dose_c_min: float
    exposure_gap_minutes: float
    cold_dose_budget_c_min: Optional[float]
    heat_dose_budget_c_min: Optional[float]
    max_integration_interval_minutes: Optional[float]
    cold_budget_usage: Optional[float]
    heat_budget_usage: Optional[float]
    last_event_time: Optional[datetime]
    last_temperature_c: Optional[float]
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
