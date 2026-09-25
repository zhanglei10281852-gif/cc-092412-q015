from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RESOURCE_TYPES = ("resident", "affair", "petition", "announcement", "audit_event")

ResourceType = Literal["resident", "affair", "petition", "announcement", "audit_event"]


class RetentionPolicyUpsert(BaseModel):
    resource_type: ResourceType
    status_filter: str | None = Field(default=None, max_length=20)
    retention_days: int = Field(ge=0, le=36500)
    enabled: bool = True
    description: str = Field(default="", max_length=500)

    @field_validator("status_filter")
    @classmethod
    def blank_status_becomes_none(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value


class HoldCreateRequest(BaseModel):
    resource_type: ResourceType
    resource_id: int = Field(ge=1)
    scope: Literal["record", "chain"] = "record"
    reason: str = Field(min_length=1, max_length=500)
    expires_at: str = Field(min_length=10, max_length=40)


class HoldReleaseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class PlanGenerateRequest(BaseModel):
    resource_types: list[ResourceType] | None = None
    notes: str = Field(default="", max_length=500)

    @field_validator("resource_types")
    @classmethod
    def unique_resource_types(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("资源类型不能重复")
        return value


class PlanExecuteRequest(BaseModel):
    batch_size: int | None = Field(default=None, ge=1, le=500)
    max_batches: int | None = Field(default=None, ge=1, le=1000)
