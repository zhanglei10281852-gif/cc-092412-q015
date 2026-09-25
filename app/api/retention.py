from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.pagination import Page
from app.core.security import Principal
from app.database import get_connection
from app.schemas.retention import (
    CandidateRequest,
    ConfirmRequest,
    BatchCancel,
    FreezeCreate,
    FreezeRelease,
    RetentionPolicyUpsert,
)
from app.services.retention import RetentionService

router = APIRouter(prefix="/api/retention", tags=["数据保留与清理"])


def service() -> RetentionService:
    # 写操作在服务内部以 IMMEDIATE 事务提交，保证候选清单、执行结果与审计同进同退
    return RetentionService(get_connection())


# ---------------------------------------------------------------------- 策略


@router.get("/policies")
def list_policies(principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_policies(principal)


@router.put("/policies", status_code=200)
def upsert_policy(data: RetentionPolicyUpsert, principal: Principal = Depends(current_principal)) -> dict:
    return service().upsert_policy(principal, data.model_dump())


# ---------------------------------------------------------------------- 冻结


@router.post("/freezes", status_code=201)
def create_freeze(data: FreezeCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().create_freeze(principal, data.model_dump())


@router.get("/freezes")
def list_freezes(
    active_only: bool = True,
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    return service().list_freezes(principal, active_only=active_only)


@router.get("/freezes/{freeze_id}")
def get_freeze(freeze_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().get_freeze(principal, freeze_id)


@router.post("/freezes/{freeze_id}/release")
def release_freeze(freeze_id: int, data: FreezeRelease, principal: Principal = Depends(current_principal)) -> dict:
    return service().release_freeze(principal, freeze_id, data.reason)


# ---------------------------------------------------------------------- 批次


@router.post("/batches/candidates", status_code=201)
def create_candidates(data: CandidateRequest, principal: Principal = Depends(current_principal)) -> dict:
    return service().create_candidates(principal, data.resource_types, data.note)


@router.get("/batches")
def list_batches(principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_batches(principal)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().get_batch(principal, batch_id)


@router.get("/batches/{batch_id}/items")
def list_batch_items(
    batch_id: int,
    decision: str | None = Query(None, pattern="^(delete|retain)$"),
    state: str | None = Query(None, pattern="^(pending|retained|deleted|skipped)$"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
) -> dict:
    pagination = Page(page, size)
    return service().list_items(
        principal, batch_id, decision=decision, state=state, limit=size, offset=pagination.offset
    )


@router.post("/batches/{batch_id}/confirm")
def confirm_batch(batch_id: int, data: ConfirmRequest, principal: Principal = Depends(current_principal)) -> dict:
    return service().confirm_batch(principal, batch_id, data.authorization_note)


@router.post("/batches/{batch_id}/execute")
def execute_batch(
    batch_id: int,
    limit: int = Query(200, ge=1, le=1000),
    principal: Principal = Depends(current_principal),
) -> dict:
    return service().execute_batch(principal, batch_id, limit=limit)


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(batch_id: int, data: BatchCancel, principal: Principal = Depends(current_principal)) -> dict:
    return service().cancel_batch(principal, batch_id, data.reason)


@router.get("/batches/{batch_id}/reconcile")
def reconcile_batch(batch_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().reconcile(principal, batch_id)


# -------------------------------------------------------------------- 解释


@router.get("/explain/{resource_type}/{resource_id}")
def explain(resource_type: str, resource_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().explain(principal, resource_type, resource_id)
