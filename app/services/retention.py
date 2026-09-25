from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditContext, AuditService


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    table: str
    label: str
    age_column: str
    status_column: str | None
    active_statuses: tuple[str, ...]
    allowed_status_filters: tuple[str, ...]


# 资源注册表：保留策略与冻结可作用的资源类型。
# active_statuses 表示“仍被有效业务引用”的状态，处于这些状态的记录不得清理。
REGISTRY: dict[str, ResourceSpec] = {
    "resident": ResourceSpec("residents", "居民档案", "updated_at", None, (), ()),
    "affair": ResourceSpec("affairs", "政务事务", "updated_at", "status", ("待受理", "办理中"), ("待受理", "办理中", "已办结", "已退回")),
    "petition": ResourceSpec(
        "petitions",
        "信访件",
        "updated_at",
        "status",
        ("待签收", "待分派", "办理中", "待审核", "退回重办", "复查中"),
        ("待签收", "待分派", "办理中", "待审核", "已办结", "退回重办", "复查中", "复查完结"),
    ),
    "announcement": ResourceSpec("announcements", "公告", "created_at", None, (), ()),
    "audit_event": ResourceSpec("audit_events", "审计事件", "created_at", None, (), ()),
}

# 删除顺序：先删子女方记录，事务删除后居民才可能解除引用。
DELETE_ORDER: tuple[str, ...] = ("petition", "affair", "resident", "announcement", "audit_event")


class RetentionService:
    """按资源类型与状态配置保留策略，支持冻结、候选清单、分批执行与对账。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.settings = Settings.load()
        self.audit = AuditService(connection, self.clock)

    @contextmanager
    def _unit_of_work(self) -> Iterator[None]:
        """单批事务：每批提交一次，中断后从已提交边界继续。"""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    # ------------------------------------------------------------------
    # 保留策略
    # ------------------------------------------------------------------
    def list_policies(self, principal: Principal) -> list[dict]:
        principal.require("retention.read")
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM retention_policies ORDER BY resource_type, (status_filter IS NULL), status_filter"
            ).fetchall()
        ]

    def upsert_policy(
        self,
        principal: Principal,
        *,
        resource_type: str,
        status_filter: str | None,
        retention_days: int,
        enabled: bool,
        description: str,
    ) -> dict:
        principal.require("retention.write")
        spec = self._spec(resource_type)
        if status_filter is not None:
            if spec.status_column is None:
                raise ValidationError(f"{spec.label}没有状态字段，保留策略不能按状态过滤")
            if status_filter not in spec.allowed_status_filters:
                raise ValidationError(f"{spec.label}不支持状态 {status_filter}")
        now = to_storage(self.clock.now())
        existing = self.connection.execute(
            "SELECT * FROM retention_policies WHERE resource_type=? AND IFNULL(status_filter,'')=IFNULL(?,'')",
            (resource_type, status_filter),
        ).fetchone()
        before = dict(existing) if existing else None
        if existing:
            self.connection.execute(
                "UPDATE retention_policies SET retention_days=?,enabled=?,description=?,updated_at=? WHERE id=?",
                (retention_days, 1 if enabled else 0, description, now, existing["id"]),
            )
            policy_id = existing["id"]
            action = "retention.policy.update"
        else:
            cursor = self.connection.execute(
                "INSERT INTO retention_policies(resource_type,status_filter,retention_days,enabled,description,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (resource_type, status_filter, retention_days, 1 if enabled else 0, description, principal.user_id, now, now),
            )
            policy_id = int(cursor.lastrowid)
            action = "retention.policy.create"
        after = dict(self.connection.execute("SELECT * FROM retention_policies WHERE id=?", (policy_id,)).fetchone())
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=action,
            resource_type="retention_policy",
            resource_id=policy_id,
            before=before,
            after=after,
        )
        return after

    def delete_policy(self, principal: Principal, policy_id: int) -> dict:
        principal.require("retention.write")
        existing = self.connection.execute("SELECT * FROM retention_policies WHERE id=?", (policy_id,)).fetchone()
        if existing is None:
            raise NotFoundError("保留策略不存在")
        self.connection.execute("DELETE FROM retention_policies WHERE id=?", (policy_id,))
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="retention.policy.delete",
            resource_type="retention_policy",
            resource_id=policy_id,
            before=dict(existing),
        )
        return {"deleted_policy": policy_id}

    # ------------------------------------------------------------------
    # 冻结
    # ------------------------------------------------------------------
    def create_hold(
        self,
        principal: Principal,
        *,
        resource_type: str,
        resource_id: int,
        scope: str,
        reason: str,
        expires_at: str,
    ) -> dict:
        principal.require("retention.write")
        spec = self._spec(resource_type)
        if self.connection.execute(f"SELECT 1 FROM {spec.table} WHERE id=?", (resource_id,)).fetchone() is None:
            raise NotFoundError(f"{spec.label}不存在")
        moment = self._parse_moment(expires_at, "冻结期限")
        now = self.clock.now()
        if moment <= now:
            raise ValidationError("冻结期限必须晚于当前时间")
        now_str = to_storage(now)
        cursor = self.connection.execute(
            "INSERT INTO retention_holds(resource_type,resource_id,scope,reason,expires_at,created_by,created_by_name,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (resource_type, resource_id, scope, reason.strip(), to_storage(moment), principal.user_id, principal.display_name, now_str),
        )
        hold = dict(self.connection.execute("SELECT * FROM retention_holds WHERE id=?", (cursor.lastrowid,)).fetchone())
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="retention.hold.create",
            resource_type="retention_hold",
            resource_id=hold["id"],
            after=hold,
            metadata={"protected": sorted(f"{rt}/{rid}" for rt, rid in self._expand_scope(hold))},
        )
        return hold

    def list_holds(
        self,
        principal: Principal,
        *,
        active_only: bool,
        resource_type: str | None = None,
        resource_id: int | None = None,
    ) -> list[dict]:
        principal.require("retention.read")
        conditions: list[str] = []
        params: list[Any] = []
        if active_only:
            conditions.append("released_at IS NULL AND expires_at>?")
            params.append(to_storage(self.clock.now()))
        if resource_type is not None:
            conditions.append("resource_type=?")
            params.append(resource_type)
        if resource_id is not None:
            conditions.append("resource_id=?")
            params.append(resource_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM retention_holds" + where + " ORDER BY id DESC", tuple(params)
            ).fetchall()
        ]

    def release_hold(self, principal: Principal, hold_id: int, reason: str) -> dict:
        principal.require("retention.write")
        hold = self.connection.execute("SELECT * FROM retention_holds WHERE id=?", (hold_id,)).fetchone()
        if hold is None:
            raise NotFoundError("冻结记录不存在")
        if hold["released_at"] is not None:
            raise ConflictError("冻结已解除")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE retention_holds SET released_at=?,released_by_name=?,release_reason=? WHERE id=?",
            (now, principal.display_name, reason.strip(), hold_id),
        )
        after = dict(self.connection.execute("SELECT * FROM retention_holds WHERE id=?", (hold_id,)).fetchone())
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="retention.hold.release",
            resource_type="retention_hold",
            resource_id=hold_id,
            before=dict(hold),
            after=after,
        )
        return after

    # ------------------------------------------------------------------
    # 候选清单
    # ------------------------------------------------------------------
    def generate_plan(self, principal: Principal, *, resource_types: list[str] | None, notes: str) -> dict:
        principal.require("retention.write")
        now = self.clock.now()
        policies = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM retention_policies WHERE enabled=1 "
                "ORDER BY resource_type, (status_filter IS NULL), status_filter"
            ).fetchall()
        ]
        if resource_types:
            for resource_type in resource_types:
                self._spec(resource_type)
            policies = [policy for policy in policies if policy["resource_type"] in resource_types]
        if not policies:
            raise ValidationError("没有已启用的保留策略，无法生成候选清单")
        snapshot = [
            {
                "policy_id": policy["id"],
                "resource_type": policy["resource_type"],
                "status_filter": policy["status_filter"],
                "retention_days": policy["retention_days"],
                "description": policy["description"],
            }
            for policy in policies
        ]
        fingerprint = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        protected = self._protected_set(now)
        now_str = to_storage(now)
        cursor = self.connection.execute(
            "INSERT INTO cleanup_plans(status,policy_fingerprint,policies_json,notes,generated_by,generated_by_name,created_at) "
            "VALUES('draft',?,?,?,?,?,?)",
            (fingerprint, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), notes.strip(), principal.user_id, principal.display_name, now_str),
        )
        plan_id = int(cursor.lastrowid)
        seq = 0
        for resource_type in DELETE_ORDER:
            spec = REGISTRY[resource_type]
            for policy in [item for item in policies if item["resource_type"] == resource_type]:
                cutoff = to_storage(now - timedelta(days=policy["retention_days"]))
                query = (
                    f"SELECT t.id FROM {spec.table} t WHERE datetime(t.{spec.age_column}) < datetime(?)"
                    " AND NOT EXISTS ("
                    "SELECT 1 FROM cleanup_plan_items i JOIN cleanup_plans p ON p.id=i.plan_id "
                    "WHERE i.resource_type=? AND i.resource_id=t.id AND p.status IN ('draft','confirmed','executing'))"
                )
                params: list[Any] = [cutoff, resource_type]
                if policy["status_filter"] is not None:
                    query += " AND t.status=?"
                    params.append(policy["status_filter"])
                query += " ORDER BY t.id"
                for row in self.connection.execute(query, tuple(params)).fetchall():
                    resource_id = int(row[0])
                    if (resource_type, resource_id) in protected:
                        continue
                    seq += 1
                    self.connection.execute(
                        "INSERT OR IGNORE INTO cleanup_plan_items(plan_id,seq,resource_type,resource_id,policy_id,retention_days,cutoff_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (plan_id, seq, resource_type, resource_id, policy["id"], policy["retention_days"], cutoff),
                    )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name, correlation_id=f"cleanup-plan-{plan_id}"),
            action="retention.plan.generate",
            resource_type="cleanup_plan",
            resource_id=plan_id,
            metadata={"candidates": seq, "policy_fingerprint": fingerprint},
        )
        return self._plan_detail(plan_id)

    def list_plans(self, principal: Principal, *, limit: int, offset: int) -> list[dict]:
        principal.require("retention.read")
        rows = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM cleanup_plans ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        ]
        for row in rows:
            row["totals"] = self._plan_totals(row["id"])
        return rows

    def plan_detail(self, principal: Principal, plan_id: int, *, limit: int = 100, offset: int = 0) -> dict:
        principal.require("retention.read")
        return self._plan_detail(plan_id, limit=limit, offset=offset)

    def _plan_detail(self, plan_id: int, *, limit: int = 100, offset: int = 0) -> dict:
        plan = self._require_plan(plan_id)
        items = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM cleanup_plan_items WHERE plan_id=? ORDER BY seq LIMIT ? OFFSET ?",
                (plan_id, limit, offset),
            ).fetchall()
        ]
        return {
            "plan": plan,
            "policies": json.loads(plan["policies_json"]),
            "totals": self._plan_totals(plan_id),
            "items": items,
        }

    def confirm_plan(self, principal: Principal, plan_id: int) -> dict:
        principal.require("retention.confirm")
        plan = self._require_plan(plan_id)
        if plan["status"] != "draft":
            raise ConflictError(f"清单当前状态为 {plan['status']}，只有待确认状态可以确认")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE cleanup_plans SET status='confirmed',confirmed_by_name=?,confirmed_at=? WHERE id=?",
            (principal.display_name, now, plan_id),
        )
        after = self._require_plan(plan_id)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name, correlation_id=f"cleanup-plan-{plan_id}"),
            action="retention.plan.confirm",
            resource_type="cleanup_plan",
            resource_id=plan_id,
            before={"status": plan["status"]},
            after={"status": "confirmed", "confirmed_by_name": principal.display_name},
        )
        return after

    def cancel_plan(self, principal: Principal, plan_id: int) -> dict:
        principal.require("retention.write")
        plan = self._require_plan(plan_id)
        if plan["status"] not in ("draft", "confirmed", "executing", "failed"):
            raise ConflictError(f"清单当前状态为 {plan['status']}，不能取消")
        self.connection.execute("UPDATE cleanup_plans SET status='cancelled' WHERE id=?", (plan_id,))
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name, correlation_id=f"cleanup-plan-{plan_id}"),
            action="retention.plan.cancel",
            resource_type="cleanup_plan",
            resource_id=plan_id,
            before={"status": plan["status"]},
            after={"status": "cancelled"},
        )
        return self._require_plan(plan_id)

    # ------------------------------------------------------------------
    # 分批执行（每批一个事务，中断后从已提交边界继续；必须在事务外调用）
    # ------------------------------------------------------------------
    def execute_plan(
        self,
        principal: Principal,
        plan_id: int,
        *,
        batch_size: int | None = None,
        max_batches: int | None = None,
    ) -> dict:
        principal.require("retention.execute")
        plan = self._require_plan(plan_id)
        if plan["status"] not in ("confirmed", "executing", "failed", "completed"):
            raise ConflictError(f"清单当前状态为 {plan['status']}，不能执行")
        size = batch_size or self.settings.cleanup_batch_size
        batches: list[dict] = []
        try:
            if plan["status"] != "completed":
                with self._unit_of_work():
                    self.connection.execute(
                        "UPDATE cleanup_plans SET status='executing',error_message=NULL WHERE id=?", (plan_id,)
                    )
                while max_batches is None or len(batches) < max_batches:
                    batch = self._execute_batch(plan_id, size, principal)
                    if batch is None:
                        break
                    batches.append(batch)
        except Exception as exc:
            with self._unit_of_work():
                self.connection.execute(
                    "UPDATE cleanup_plans SET status='failed',error_message=? WHERE id=?",
                    (str(exc)[:500], plan_id),
                )
            raise
        with self._unit_of_work():
            pending = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM cleanup_plan_items WHERE plan_id=? AND status='pending'", (plan_id,)
                ).fetchone()[0]
            )
            current = self.connection.execute("SELECT status FROM cleanup_plans WHERE id=?", (plan_id,)).fetchone()[0]
            if pending == 0 and current != "completed":
                self.connection.execute(
                    "UPDATE cleanup_plans SET status='completed',finished_at=? WHERE id=?", (to_storage(self.clock.now()), plan_id)
                )
                self.audit.record(
                    AuditContext(principal.user_id, principal.display_name, correlation_id=f"cleanup-plan-{plan_id}"),
                    action="retention.plan.execute",
                    resource_type="cleanup_plan",
                    resource_id=plan_id,
                    metadata={"totals": self._plan_totals(plan_id)},
                )
        return {"plan": self._require_plan(plan_id), "batches": batches, "totals": self._plan_totals(plan_id)}

    def _execute_batch(self, plan_id: int, batch_size: int, principal: Principal) -> dict | None:
        with self._unit_of_work():
            items = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM cleanup_plan_items WHERE plan_id=? AND status='pending' ORDER BY seq LIMIT ?",
                    (plan_id, batch_size),
                ).fetchall()
            ]
            if not items:
                return None
            now = self.clock.now()
            now_str = to_storage(now)
            batch_no = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(batch_no),0) FROM cleanup_batches WHERE plan_id=?", (plan_id,)
                ).fetchone()[0]
            ) + 1
            cursor = self.connection.execute(
                "INSERT INTO cleanup_batches(plan_id,batch_no,status,executor_name,started_at) VALUES(?,?,'running',?,?)",
                (plan_id, batch_no, principal.display_name, now_str),
            )
            batch_id = int(cursor.lastrowid)
            protected = self._protected_set(now)
            results: list[dict] = []
            for item in items:
                decision, reason = self._process_item(item, protected)
                self.connection.execute(
                    "UPDATE cleanup_plan_items SET status=?,reason=?,batch_id=?,processed_at=? WHERE id=?",
                    (decision, reason, batch_id, now_str, item["id"]),
                )
                results.append(
                    {
                        "seq": item["seq"],
                        "resource_type": item["resource_type"],
                        "resource_id": item["resource_id"],
                        "decision": decision,
                        "reason": reason,
                    }
                )
            deleted = sum(1 for result in results if result["decision"] == "deleted")
            self.connection.execute(
                "UPDATE cleanup_batches SET status='committed',processed_count=?,deleted_count=?,skipped_count=?,committed_at=? "
                "WHERE id=?",
                (len(results), deleted, len(results) - deleted, now_str, batch_id),
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name, correlation_id=f"cleanup-plan-{plan_id}-batch-{batch_no}"),
                action="retention.batch.execute",
                resource_type="cleanup_batch",
                resource_id=batch_id,
                metadata={
                    "plan_id": plan_id,
                    "batch_no": batch_no,
                    "processed": len(results),
                    "deleted": deleted,
                    "skipped": len(results) - deleted,
                    "items": results,
                },
            )
            return {
                "batch_id": batch_id,
                "batch_no": batch_no,
                "processed": len(results),
                "deleted": deleted,
                "skipped": len(results) - deleted,
            }

    def _process_item(self, item: dict, protected: set[tuple[str, int]]) -> tuple[str, str | None]:
        resource_type = item["resource_type"]
        resource_id = int(item["resource_id"])
        spec = REGISTRY[resource_type]
        if (resource_type, resource_id) in protected:
            return "skipped_frozen", self._frozen_reason(resource_type, resource_id)
        row = self.connection.execute(f"SELECT * FROM {spec.table} WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            return "skipped_missing", "记录已不存在，可能已被其他清单清理"
        blocker = self._reference_block(resource_type, dict(row))
        if blocker is not None:
            return "skipped_referenced", blocker
        self.connection.execute(f"DELETE FROM {spec.table} WHERE id=?", (resource_id,))
        return "deleted", None

    # ------------------------------------------------------------------
    # 解释与对账
    # ------------------------------------------------------------------
    def explain(self, principal: Principal, *, resource_type: str, resource_id: int) -> dict:
        principal.require("retention.read")
        spec = self._spec(resource_type)
        now = self.clock.now()
        row = self.connection.execute(f"SELECT * FROM {spec.table} WHERE id=?", (resource_id,)).fetchone()
        record = dict(row) if row is not None else None
        holds = self._covering_holds(resource_type, resource_id, now)
        reference_block = self._reference_block(resource_type, record) if record is not None else None
        policies = []
        for row_policy in self.connection.execute(
            "SELECT * FROM retention_policies WHERE resource_type=? ORDER BY (status_filter IS NULL), status_filter",
            (resource_type,),
        ).fetchall():
            policy = dict(row_policy)
            cutoff = to_storage(now - timedelta(days=policy["retention_days"]))
            entry = {
                "policy_id": policy["id"],
                "status_filter": policy["status_filter"],
                "retention_days": policy["retention_days"],
                "enabled": bool(policy["enabled"]),
                "cutoff_at": cutoff,
                "matches": None,
            }
            if record is not None:
                status_ok = policy["status_filter"] is None or record.get(spec.status_column or "") == policy["status_filter"]
                moment = from_storage(record[spec.age_column])
                cutoff_moment = from_storage(cutoff)
                entry["matches"] = bool(status_ok and moment is not None and cutoff_moment is not None and moment < cutoff_moment)
            policies.append(entry)
        history = [
            dict(item)
            for item in self.connection.execute(
                "SELECT i.id AS item_id,i.plan_id,i.status AS decision,i.reason,i.processed_at,i.batch_id,p.status AS plan_status "
                "FROM cleanup_plan_items i JOIN cleanup_plans p ON p.id=i.plan_id "
                "WHERE i.resource_type=? AND i.resource_id=? ORDER BY i.id",
                (resource_type, resource_id),
            ).fetchall()
        ]
        if record is None:
            verdict = "absent"
            verdict_text = "记录不存在" + ("，已被清单清理" if any(item["decision"] == "deleted" for item in history) else "")
        elif holds:
            verdict = "retained_hold"
            verdict_text = "被冻结保留：" + "；".join(hold["reason"] for hold in holds)
        elif reference_block is not None:
            verdict = "retained_reference"
            verdict_text = reference_block
        elif any(policy["enabled"] and policy["matches"] for policy in policies):
            verdict = "eligible"
            verdict_text = "已超出保留期限，可被纳入清理清单"
        else:
            verdict = "retained_policy"
            verdict_text = "未达到保留期限或没有启用的匹配策略"
        return {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "exists": record is not None,
            "verdict": verdict,
            "verdict_text": verdict_text,
            "holds": holds,
            "reference_block": reference_block,
            "policies": policies,
            "plan_history": history,
            "explained_at": to_storage(now),
        }

    def reconcile(self, principal: Principal, plan_id: int) -> dict:
        principal.require("retention.read")
        plan = self._require_plan(plan_id)
        items = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM cleanup_plan_items WHERE plan_id=? ORDER BY seq", (plan_id,)
            ).fetchall()
        ]
        batches = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM cleanup_batches WHERE plan_id=? ORDER BY batch_no", (plan_id,)
            ).fetchall()
        ]
        discrepancies: list[dict] = []
        batch_reports: list[dict] = []
        for batch in batches:
            linked = [item for item in items if item["batch_id"] == batch["id"]]
            deleted = sum(1 for item in linked if item["status"] == "deleted")
            problems: list[str] = []
            if batch["status"] != "committed":
                problems.append("批次未提交")
            if batch["processed_count"] != len(linked):
                problems.append(f"批次记录处理数 {batch['processed_count']} 与清单条目数 {len(linked)} 不一致")
            if batch["deleted_count"] != deleted:
                problems.append(f"批次删除数 {batch['deleted_count']} 与清单删除数 {deleted} 不一致")
            if batch["skipped_count"] != len(linked) - deleted:
                problems.append(f"批次跳过数 {batch['skipped_count']} 与清单跳过数 {len(linked) - deleted} 不一致")
            events = self.connection.execute(
                "SELECT * FROM audit_events WHERE action='retention.batch.execute' AND correlation_id=?",
                (f"cleanup-plan-{plan_id}-batch-{batch['batch_no']}",),
            ).fetchall()
            if len(events) != 1:
                problems.append(f"批次审计记录数量为 {len(events)}，应为 1")
            else:
                metadata = json.loads(events[0]["metadata_json"])
                recorded = {
                    (entry["resource_type"], entry["resource_id"], entry["decision"])
                    for entry in metadata.get("items", [])
                }
                actual = {(item["resource_type"], item["resource_id"], item["status"]) for item in linked}
                if recorded != actual:
                    problems.append("批次审计明细与清单条目不一致")
            if problems:
                discrepancies.append({"batch_no": batch["batch_no"], "problems": problems})
            batch_reports.append(
                {
                    "batch_id": batch["id"],
                    "batch_no": batch["batch_no"],
                    "status": batch["status"],
                    "processed": len(linked),
                    "deleted": deleted,
                    "skipped": len(linked) - deleted,
                    "audit_events": len(events),
                    "committed_at": batch["committed_at"],
                }
            )
        for item in items:
            problems = []
            if item["status"] == "pending" and item["batch_id"] is not None:
                problems.append("待处理条目不应关联批次")
            if item["status"] != "pending" and item["batch_id"] is None:
                problems.append("已处理条目缺少批次关联")
            if item["status"] == "deleted":
                spec = REGISTRY[item["resource_type"]]
                exists = self.connection.execute(
                    f"SELECT 1 FROM {spec.table} WHERE id=?", (item["resource_id"],)
                ).fetchone()
                if exists is not None:
                    problems.append("条目标记已删除但记录仍存在")
            if problems:
                discrepancies.append({"item_id": item["id"], "problems": problems})
        return {
            "plan_id": plan_id,
            "plan_status": plan["status"],
            "ok": not discrepancies,
            "totals": self._plan_totals(plan_id),
            "batches": batch_reports,
            "discrepancies": discrepancies,
            "checked_at": to_storage(self.clock.now()),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _spec(self, resource_type: str) -> ResourceSpec:
        spec = REGISTRY.get(resource_type)
        if spec is None:
            raise ValidationError(f"不支持的资源类型：{resource_type}")
        return spec

    def _require_plan(self, plan_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM cleanup_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("清理清单不存在")
        return dict(row)

    def _plan_totals(self, plan_id: int) -> dict:
        counts = {"pending": 0, "deleted": 0, "skipped_frozen": 0, "skipped_referenced": 0, "skipped_missing": 0}
        for row in self.connection.execute(
            "SELECT status,COUNT(*) AS total FROM cleanup_plan_items WHERE plan_id=? GROUP BY status", (plan_id,)
        ).fetchall():
            counts[row["status"]] = int(row["total"])
        counts["total"] = sum(counts.values())
        return counts

    def _parse_moment(self, value: str, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValidationError(f"{label}必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _active_holds(self, now: datetime) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM retention_holds WHERE released_at IS NULL AND expires_at>?", (to_storage(now),)
            ).fetchall()
        ]

    def _protected_set(self, now: datetime) -> set[tuple[str, int]]:
        protected: set[tuple[str, int]] = set()
        for hold in self._active_holds(now):
            protected |= self._expand_scope(hold)
        return protected

    def _expand_scope(self, hold: dict) -> set[tuple[str, int]]:
        resource_type = hold["resource_type"]
        resource_id = int(hold["resource_id"])
        if hold["scope"] == "record":
            return {(resource_type, resource_id)}
        return self._expand_chain(resource_type, resource_id)

    def _expand_chain(self, resource_type: str, resource_id: int) -> set[tuple[str, int]]:
        protected = {(resource_type, resource_id)}
        if resource_type == "resident":
            affair_ids = [
                int(row[0])
                for row in self.connection.execute("SELECT id FROM affairs WHERE applicant_id=?", (resource_id,)).fetchall()
            ]
            protected |= {("affair", affair_id) for affair_id in affair_ids}
            protected |= self._audit_events_of("resident", resource_id)
            for affair_id in affair_ids:
                protected |= self._audit_events_of("affair", affair_id)
        elif resource_type == "affair":
            row = self.connection.execute("SELECT applicant_id FROM affairs WHERE id=?", (resource_id,)).fetchone()
            if row is not None:
                protected.add(("resident", int(row[0])))
            protected |= self._audit_events_of("affair", resource_id)
        elif resource_type in ("petition", "announcement"):
            protected |= self._audit_events_of(resource_type, resource_id)
        return protected

    def _audit_events_of(self, resource_type: str, resource_id: int) -> set[tuple[str, int]]:
        return {
            ("audit_event", int(row[0]))
            for row in self.connection.execute(
                "SELECT id FROM audit_events WHERE resource_type=? AND resource_id=?",
                (resource_type, str(resource_id)),
            ).fetchall()
        }

    def _covering_holds(self, resource_type: str, resource_id: int, now: datetime) -> list[dict]:
        covering: list[dict] = []
        for hold in self._active_holds(now):
            via = None
            if hold["resource_type"] == resource_type and int(hold["resource_id"]) == resource_id:
                via = "direct"
            elif hold["scope"] == "chain" and (resource_type, resource_id) in self._expand_chain(
                hold["resource_type"], int(hold["resource_id"])
            ):
                via = f"chain:{hold['resource_type']}/{hold['resource_id']}"
            if via is not None:
                covering.append(
                    {
                        "hold_id": hold["id"],
                        "scope": hold["scope"],
                        "via": via,
                        "reason": hold["reason"],
                        "expires_at": hold["expires_at"],
                        "created_by_name": hold["created_by_name"],
                    }
                )
        return covering

    def _frozen_reason(self, resource_type: str, resource_id: int) -> str:
        holds = self._covering_holds(resource_type, resource_id, self.clock.now())
        details = "；".join(f"冻结#{hold['hold_id']}（{hold['reason']}，{hold['expires_at']}到期）" for hold in holds)
        return f"记录被冻结：{details}" if details else "记录被冻结"

    def _reference_block(self, resource_type: str, record: dict | None) -> str | None:
        if record is None:
            return None
        spec = REGISTRY[resource_type]
        if resource_type == "resident":
            total = int(
                self.connection.execute("SELECT COUNT(*) FROM affairs WHERE applicant_id=?", (record["id"],)).fetchone()[0]
            )
            if total > 0:
                active = int(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM affairs WHERE applicant_id=? AND status IN ('待受理','办理中')",
                        (record["id"],),
                    ).fetchone()[0]
                )
                return f"居民仍被 {total} 条事务引用（其中 {active} 条在办理）"
            return None
        if spec.status_column is not None and record.get(spec.status_column) in spec.active_statuses:
            return f"{spec.label}仍处于{record[spec.status_column]}状态，属于有效业务"
        return None
