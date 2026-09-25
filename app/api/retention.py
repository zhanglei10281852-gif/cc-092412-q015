from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.pagination import Page, page_result
from app.core.security import Principal
from app.database import get_connection, transaction
from app.schemas.retention import (
    HoldCreateRequest,
    HoldReleaseRequest,
    PlanExecuteRequest,
    PlanGenerateRequest,
    RetentionPolicyUpsert,
)
from app.services.retention import RetentionService

router = APIRouter(prefix="/api/retention", tags=["数据保留与清理"])


@router.get("/policies")
def list_policies(principal: Principal = Depends(current_principal)) -> dict:
    return {"data": RetentionService(get_connection()).list_policies(principal)}


@router.put("/policies")
def upsert_policy(data: RetentionPolicyUpsert, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return RetentionService(connection).upsert_policy(
            principal,
            resource_type=data.resource_type,
            status_filter=data.status_filter,
            retention_days=data.retention_days,
            enabled=data.enabled,
            description=data.description,
        )


@router.delete("/policies/{policy_id}")
def delete_policy(policy_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return RetentionService(connection).delete_policy(principal, policy_id)


@router.get("/holds")
def list_holds(
    active_only: bool = True,
    resource_type: str | None = None,
    resource_id: int | None = None,
    principal: Principal = Depends(current_principal),
) -> dict:
    return {
        "data": RetentionService(get_connection()).list_holds(
            principal, active_only=active_only, resource_type=resource_type, resource_id=resource_id
        )
    }


@router.post("/holds", status_code=201)
def create_hold(data: HoldCreateRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return RetentionService(connection).create_hold(
            principal,
            resource_type=data.resource_type,
            resource_id=data.resource_id,
            scope=data.scope,
            reason=data.reason,
            expires_at=data.expires_at,
        )


@router.post("/holds/{hold_id}/release")
def release_hold(hold_id: int, data: HoldReleaseRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return RetentionService(connection).release_hold(principal, hold_id, data.reason)


@router.get("/plans")
def list_plans(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("retention.read")
    pagination = Page(page, size)
    connection = get_connection()
    total = int(connection.execute("SELECT COUNT(*) FROM cleanup_plans").fetchone()[0])
    rows = RetentionService(connection).list_plans(principal, limit=size, offset=pagination.offset)
    return page_result(total=total, page=pagination, rows=rows)


@router.post("/plans", status_code=201)
def generate_plan(data: PlanGenerateRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return RetentionService(connection).generate_plan(
            principal, resource_types=data.resource_types, notes=data.notes
        )


@router.get("/plans/{plan_id}")
def plan_detail(
    plan_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    pagination = Page(page, size)
    return RetentionService(get_connection()).plan_detail(
        principal, plan_id, limit=size, offset=pagination.offset
    )


@router.post("/plans/{plan_id}/confirm")
def confirm_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return RetentionService(connection).confirm_plan(principal, plan_id)


@router.post("/plans/{plan_id}/cancel")
def cancel_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return RetentionService(connection).cancel_plan(principal, plan_id)


@router.post("/plans/{plan_id}/execute")
def execute_plan(plan_id: int, data: PlanExecuteRequest, principal: Principal = Depends(current_principal)) -> dict:
    # 执行按批提交，不能包裹在单一事务里，否则中断后无法从已提交边界继续。
    return RetentionService(get_connection()).execute_plan(
        principal, plan_id, batch_size=data.batch_size, max_batches=data.max_batches
    )


@router.get("/plans/{plan_id}/reconcile")
def reconcile_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return RetentionService(get_connection()).reconcile(principal, plan_id)


@router.get("/explain")
def explain(
    resource_type: str,
    resource_id: int,
    principal: Principal = Depends(current_principal),
) -> dict:
    return RetentionService(get_connection()).explain(
        principal, resource_type=resource_type, resource_id=resource_id
    )
