from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

RESOURCE_TYPES = ("residents", "affairs", "petitions", "announcements")


class RetentionPolicyUpsert(BaseModel):
    resource_type: str = Field(..., description="资源类型")
    status: str = Field("*", description="业务状态，* 表示全部状态")
    retain_days: Optional[int] = Field(None, ge=1, description="保留天数；空表示永久保留")
    is_active: bool = True
    note: str = Field("", max_length=500)


class FreezeCreate(BaseModel):
    resource_type: str
    resource_id: int = Field(..., ge=1)
    scope: str = Field("record", description="record-仅冻结本条；chain-冻结本条及其关联链")
    reason: str = Field(..., min_length=2, max_length=500)
    retain_days: int = Field(..., ge=1, le=36500, description="冻结有效期（天）")


class FreezeRelease(BaseModel):
    reason: str = Field(..., min_length=2, max_length=500)


class CandidateRequest(BaseModel):
    resource_types: list[str] = Field(..., min_length=1, max_length=10)
    note: str = Field("", max_length=500)


class ConfirmRequest(BaseModel):
    authorization_note: str = Field(..., min_length=2, max_length=500, description="授权确认说明")


class BatchCancel(BaseModel):
    reason: str = Field(..., min_length=2, max_length=500)
