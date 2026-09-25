from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.schemas.retention import RESOURCE_TYPES
from app.services.audit import AuditContext, AuditService

# 每种资源的状态取值，None 表示该资源没有业务状态列
RESOURCE_STATUSES: dict[str, set[str] | None] = {
    "residents": None,
    "affairs": {"待受理", "办理中", "已办结", "已退回"},
    "petitions": {"待签收", "待分派", "办理中", "待审核", "已办结", "退回重办", "复查中", "复查完结"},
    "announcements": None,
}

# 仍在办理、尚未终结的状态：处于这些状态的数据永远视为“被有效业务引用”
ACTIVE_BUSINESS_STATUSES: dict[str, set[str]] = {
    "affairs": {"待受理", "办理中"},
    "petitions": {"待签收", "待分派", "办理中", "待审核", "退回重办", "复查中"},
}

DEFAULT_EXECUTE_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class ResourceRow:
    resource_type: str
    resource_id: int
    status: str | None
    anchor_time: str
    label: str


class RetentionService:
    """保留策略、冻结与清理批次领域服务。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 策略

    def list_policies(self, principal: Principal) -> list[dict]:
        principal.require("retention.read")
        rows = self.connection.execute(
            "SELECT * FROM retention_policies ORDER BY resource_type, CASE status WHEN '*' THEN 1 ELSE 0 END, status"
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_policy(self, principal: Principal, data: dict) -> dict:
        principal.require("retention.write")
        resource_type = data["resource_type"]
        status = data.get("status") or "*"
        self._validate_target(resource_type, status)
        now = to_storage(self.clock.now())
        with self._immediate():
            before = self.connection.execute(
                "SELECT * FROM retention_policies WHERE resource_type=? AND status=?", (resource_type, status)
            ).fetchone()
            self.connection.execute(
                "INSERT INTO retention_policies(resource_type,status,retain_days,is_active,note,"
                "updated_by_user_id,updated_by_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(resource_type,status) DO UPDATE SET retain_days=excluded.retain_days,"
                "is_active=excluded.is_active,note=excluded.note,updated_by_user_id=excluded.updated_by_user_id,"
                "updated_by_name=excluded.updated_by_name,updated_at=excluded.updated_at",
                (
                    resource_type,
                    status,
                    data.get("retain_days"),
                    1 if data.get("is_active", True) else 0,
                    data.get("note", ""),
                    principal.user_id,
                    principal.display_name,
                    now,
                    now,
                ),
            )
            after = self.connection.execute(
                "SELECT * FROM retention_policies WHERE resource_type=? AND status=?", (resource_type, status)
            ).fetchone()
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="retention.policy.upsert",
                resource_type="retention_policy",
                resource_id=f"{resource_type}:{status}",
                before=dict(before) if before else None,
                after=dict(after),
            )
        return dict(after)

    # ------------------------------------------------------------------ 冻结

    def create_freeze(self, principal: Principal, data: dict) -> dict:
        principal.require("retention.write")
        resource_type = data["resource_type"]
        resource_id = int(data["resource_id"])
        scope = data.get("scope", "record")
        if scope not in {"record", "chain"}:
            raise ValidationError("冻结范围只能是 record 或 chain")
        reason = data["reason"].strip()
        if not reason:
            raise ValidationError("冻结原因不能为空")
        self._require_resource_exists(resource_type, resource_id)
        now = self.clock.now()
        from datetime import timedelta

        expires_at = to_storage(now + timedelta(days=int(data["retain_days"])))
        now_text = to_storage(now)
        with self._immediate():
            cursor = self.connection.execute(
                "INSERT INTO retention_freezes(resource_type,resource_id,scope,reason,expires_at,"
                "created_by_user_id,created_by_name,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    resource_type,
                    resource_id,
                    scope,
                    reason,
                    expires_at,
                    principal.user_id,
                    principal.display_name,
                    now_text,
                ),
            )
            freeze_id = int(cursor.lastrowid)
            affected = self._collect_chain(resource_type, resource_id) if scope == "chain" else [(resource_type, resource_id)]
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="retention.freeze.create",
                resource_type=resource_type,
                resource_id=resource_id,
                after={"freeze_id": freeze_id, "scope": scope, "expires_at": expires_at, "covers": self._serialize_targets(affected)},
            )
        return self.get_freeze(principal, freeze_id)

    def release_freeze(self, principal: Principal, freeze_id: int, reason: str) -> dict:
        principal.require("retention.write")
        freeze = self._require_freeze(freeze_id)
        if freeze["released_at"] is not None:
            raise ConflictError("该冻结已解除")
        now = to_storage(self.clock.now())
        with self._immediate():
            self.connection.execute(
                "UPDATE retention_freezes SET released_at=?,released_by_user_id=?,released_by_name=?,release_reason=? WHERE id=?",
                (now, principal.user_id, principal.display_name, reason.strip(), freeze_id),
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="retention.freeze.release",
                resource_type=freeze["resource_type"],
                resource_id=freeze["resource_id"],
                before=dict(freeze),
                after={"released_at": now, "release_reason": reason},
            )
        return self._require_freeze(freeze_id)

    def get_freeze(self, principal: Principal, freeze_id: int) -> dict:
        principal.require("retention.read")
        return self._require_freeze(freeze_id)

    def list_freezes(self, principal: Principal, *, active_only: bool) -> list[dict]:
        principal.require("retention.read")
        now = to_storage(self.clock.now())
        sql = "SELECT * FROM retention_freezes"
        if active_only:
            sql += " WHERE released_at IS NULL AND expires_at>?"
            rows = self.connection.execute(sql + " ORDER BY id DESC", (now,)).fetchall()
        else:
            rows = self.connection.execute(sql + " ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def active_freeze_on(self, resource_type: str, resource_id: int, *, moment: str) -> dict | None:
        """返回 moment 时刻直接作用于该记录的有效冻结（含链冻结的传递）。"""
        rows = self.connection.execute(
            "SELECT * FROM retention_freezes WHERE resource_type=? AND resource_id=? "
            "AND released_at IS NULL AND created_at<=? AND expires_at>? ORDER BY id DESC",
            (resource_type, resource_id, moment, moment),
        ).fetchall()
        if rows:
            return dict(rows[0])
        # 链冻结：任一祖先节点上的 chain 冻结都会覆盖本记录
        for ancestor_type, ancestor_id in self._chain_ancestors(resource_type, resource_id):
            rows = self.connection.execute(
                "SELECT * FROM retention_freezes WHERE resource_type=? AND resource_id=? AND scope='chain' "
                "AND released_at IS NULL AND created_at<=? AND expires_at>? ORDER BY id DESC",
                (ancestor_type, ancestor_id, moment, moment),
            ).fetchall()
            if rows:
                freeze = dict(rows[0])
                freeze["propagated_via"] = f"{ancestor_type}:{ancestor_id}"
                return freeze
        return None

    # ------------------------------------------------------------------ 批次

    def create_candidates(self, principal: Principal, resource_types: list[str], note: str) -> dict:
        principal.require("retention.clean")
        normalized = self._validate_resource_types(resource_types)
        now = self.clock.now()
        now_text = to_storage(now)
        policies = self._active_policy_map()
        snapshot = [dict(row) for row in self.connection.execute(
            "SELECT * FROM retention_policies WHERE is_active=1 ORDER BY resource_type,status"
        ).fetchall()]
        with self._immediate():
            cursor = self.connection.execute(
                "INSERT INTO cleanup_batches(status,resource_types_json,policy_snapshot_json,note,"
                "created_by_user_id,created_by_name,created_at,updated_at) VALUES('candidate',?,?,?,?,?,?,?)",
                (json.dumps(normalized, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False),
                 note.strip(), principal.user_id, principal.display_name, now_text, now_text),
            )
            batch_id = int(cursor.lastrowid)
            counts = {"delete": 0, "retain": 0}
            # 子表（事务等）先于居民入册，保证执行时按 id 顺序先清理引用方，
            # 再删除居民，避免外键约束导致居民无法删除
            scan_order = sorted(normalized, key=lambda kind: 1 if kind == "residents" else 0)
            scanned: list[ResourceRow] = []
            for resource_type in scan_order:
                scanned.extend(self._scan_resource(resource_type))
            # 第一遍评估非居民资源；第二遍评估居民时，本批次内将一并删除的
            # 事务不再算作“仍被有效业务引用”
            co_deleting: set[tuple[str, int]] = set()
            evaluations: list[tuple[ResourceRow, dict]] = []
            for row in scanned:
                evaluation = self._evaluate(row, policies, moment=now_text, ignore_references=co_deleting)
                evaluations.append((row, evaluation))
                if row.resource_type != "residents" and evaluation["deletable"]:
                    co_deleting.add((row.resource_type, row.resource_id))
            for row, evaluation in evaluations:
                decision = "delete" if evaluation["deletable"] else "retain"
                counts[decision] += 1
                self.connection.execute(
                    "INSERT INTO cleanup_items(batch_id,resource_type,resource_id,captured_status,anchor_time,"
                    "retain_days,eligibility_time,label,decision,state,block_reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        row.resource_type,
                        row.resource_id,
                        row.status,
                        row.anchor_time,
                        evaluation["retain_days"],
                        evaluation["eligibility_time"],
                        row.label,
                        decision,
                        "pending",
                        None if evaluation["deletable"] else "；".join(evaluation["reasons"]),
                    ),
                )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="retention.batch.candidates",
                resource_type="cleanup_batch",
                resource_id=batch_id,
                after={"resource_types": normalized, **counts},
            )
        return self.get_batch(principal, batch_id)

    def confirm_batch(self, principal: Principal, batch_id: int, authorization_note: str) -> dict:
        principal.require("retention.clean")
        with self._immediate():
            batch = self._require_batch(batch_id)
            if batch["status"] != "candidate":
                raise ConflictError("只有候选状态的批次才能确认")
            now = self.clock.now()
            now_text = to_storage(now)
            # 授权确认即锁定策略快照：此后策略修改不追溯改变本批次
            snapshot = [dict(row) for row in self.connection.execute(
                "SELECT * FROM retention_policies WHERE is_active=1 ORDER BY resource_type,status"
            ).fetchall()]
            self.connection.execute(
                "UPDATE cleanup_batches SET status='confirmed',confirmed_by_user_id=?,confirmed_by_name=?,"
                "confirmed_at=?,authorization_note=?,policy_snapshot_json=?,updated_at=? WHERE id=?",
                (principal.user_id, principal.display_name, now_text, authorization_note.strip(),
                 json.dumps(snapshot, ensure_ascii=False), now_text, batch_id),
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="retention.batch.confirm",
                resource_type="cleanup_batch",
                resource_id=batch_id,
                before={"status": "candidate"},
                after={"status": "confirmed", "authorization_note": authorization_note},
            )
        return self._require_batch(batch_id)

    def execute_batch(self, principal: Principal, batch_id: int, *, limit: int = DEFAULT_EXECUTE_BATCH_SIZE) -> dict:
        """执行最多 limit 条候选；可重复调用，从已提交边界继续。"""
        principal.require("retention.clean")
        limit = max(1, min(int(limit), 1000))
        progress = {"deleted": 0, "retained": 0, "skipped": 0, "executed": 0}
        with self._immediate():
            batch = self._require_batch(batch_id)
            if batch["status"] == "candidate":
                raise ConflictError("批次尚未授权确认，不能执行")
            if batch["status"] == "cancelled":
                raise ConflictError("批次已取消，不能执行")
            if batch["status"] == "completed":
                # 重复运行直接返回既有结果，不重复计数
                return self.get_batch(principal, batch_id)
            self.connection.execute(
                "UPDATE cleanup_batches SET status='executing',updated_at=? WHERE id=?",
                (to_storage(self.clock.now()), batch_id),
            )
            moment = to_storage(self.clock.now())
            # 使用确认时锁定的策略快照，而非当前策略
            snapshot_policies = self._policy_map_from_snapshot(batch.get("policy_snapshot_json"))
            rows = self.connection.execute(
                "SELECT * FROM cleanup_items WHERE batch_id=? AND id>? ORDER BY id LIMIT ?",
                (batch_id, batch["boundary_item_id"], limit),
            ).fetchall()
            for item_row in rows:
                item = dict(item_row)
                item_id = item["id"]
                resource_type = item["resource_type"]
                resource_id = item["resource_id"]
                # 执行时以当前数据状态重新校验：冻结或有效业务引用一票否决
                current = self._fetch_resource_row(resource_type, resource_id)
                if current is None:
                    state, block_reason = "skipped", "记录已不存在"
                else:
                    evaluation = self._evaluate(current, snapshot_policies, moment=moment)
                    if evaluation["deletable"]:
                        self._delete_resource(resource_type, resource_id)
                        state, block_reason = "deleted", None
                    else:
                        state, block_reason = "retained", "；".join(evaluation["reasons"])
                executed_at = to_storage(self.clock.now())
                self.connection.execute(
                    "UPDATE cleanup_items SET state=?,block_reason=?,executed_at=? WHERE id=?",
                    (state, block_reason, executed_at, item_id),
                )
                self.connection.execute(
                    "UPDATE cleanup_batches SET boundary_item_id=?,updated_at=? WHERE id=?",
                    (item_id, executed_at, batch_id),
                )
                progress["executed"] += 1
                if state == "deleted":
                    progress["deleted"] += 1
                elif state == "retained":
                    progress["retained"] += 1
                else:
                    progress["skipped"] += 1
            pending = int(self.connection.execute(
                "SELECT COUNT(*) FROM cleanup_items WHERE batch_id=? AND state='pending'", (batch_id,)
            ).fetchone()[0])
            if pending == 0:
                finished = to_storage(self.clock.now())
                self.connection.execute(
                    "UPDATE cleanup_batches SET status='completed',completed_at=?,updated_at=? WHERE id=?",
                    (finished, finished, batch_id),
                )
            summary = self._batch_summary(batch_id)
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="retention.batch.execute",
                resource_type="cleanup_batch",
                resource_id=batch_id,
                after={**progress, "pending": pending},
            )
        return self.get_batch(principal, batch_id)

    def cancel_batch(self, principal: Principal, batch_id: int, reason: str) -> dict:
        principal.require("retention.clean")
        with self._immediate():
            batch = self._require_batch(batch_id)
            if batch["status"] in {"completed", "cancelled"}:
                raise ConflictError("已完成或已取消的批次不能再次取消")
            now = to_storage(self.clock.now())
            self.connection.execute(
                "UPDATE cleanup_batches SET status='cancelled',cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=?",
                (now, reason.strip(), now, batch_id),
            )
            self.connection.execute(
                "UPDATE cleanup_items SET state='skipped',block_reason=? WHERE batch_id=? AND state='pending'",
                (f"批次取消：{reason.strip()}", batch_id),
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="retention.batch.cancel",
                resource_type="cleanup_batch",
                resource_id=batch_id,
                after={"reason": reason},
            )
        return self._require_batch(batch_id)

    def list_batches(self, principal) -> list[dict]:
        principal.require("retention.read")
        rows = self.connection.execute("SELECT * FROM cleanup_batches ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            batch = dict(row)
            batch.update(self._batch_summary(batch["id"]))
            result.append(batch)
        return result

    def get_batch(self, principal: Principal, batch_id: int) -> dict:
        principal.require("retention.read")
        batch = self._require_batch(batch_id)
        batch.update(self._batch_summary(batch_id))
        return batch

    def list_items(
        self,
        principal: Principal,
        batch_id: int,
        *,
        decision: str | None,
        state: str | None,
        limit: int,
        offset: int,
    ) -> dict:
        principal.require("retention.read")
        self._require_batch(batch_id)
        conditions = ["batch_id=?"]
        params: list = [batch_id]
        if decision:
            conditions.append("decision=?")
            params.append(decision)
        if state:
            conditions.append("state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(conditions)
        total = int(self.connection.execute(
            "SELECT COUNT(*) FROM cleanup_items" + where, tuple(params)
        ).fetchone()[0])
        rows = self.connection.execute(
            "SELECT * FROM cleanup_items" + where + " ORDER BY id LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        ).fetchall()
        return {"total": total, "data": [dict(row) for row in rows]}

    # ------------------------------------------------------------------ 解释与对账

    def explain(self, principal: Principal, resource_type: str, resource_id: int) -> dict:
        """解释某条数据在当前策略下为何应被保留或可以清理。"""
        principal.require("retention.read")
        if resource_type not in RESOURCE_TYPES:
            raise ValidationError(f"不支持的资源类型：{resource_type}")
        row = self._fetch_resource_row(resource_type, resource_id)
        moment = to_storage(self.clock.now())
        if row is None:
            return {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "exists": False,
                "verdict": "absent",
                "reasons": ["记录不存在（可能已按既有批次清理）"],
            }
        evaluation = self._evaluate(row, self._active_policy_map(), moment=moment)
        active_freeze = self.active_freeze_on(resource_type, resource_id, moment=moment)
        latest_item = self.connection.execute(
            "SELECT ci.*, cb.status AS batch_status FROM cleanup_items ci "
            "JOIN cleanup_batches cb ON cb.id=ci.batch_id "
            "WHERE ci.resource_type=? AND ci.resource_id=? ORDER BY ci.id DESC LIMIT 1",
            (resource_type, resource_id),
        ).fetchone()
        return {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "exists": True,
            "label": row.label,
            "status": row.status,
            "anchor_time": row.anchor_time,
            "verdict": "retain" if not evaluation["deletable"] else "deletable",
            "reasons": evaluation["reasons"],
            "policy": evaluation["policy"],
            "retain_days": evaluation["retain_days"],
            "eligibility_time": evaluation["eligibility_time"],
            "active_freeze": active_freeze,
            "referenced_by": evaluation["referenced_by"],
            "latest_cleanup_item": dict(latest_item) if latest_item else None,
            "explained_at": moment,
        }

    def reconcile(self, principal: Principal, batch_id: int) -> dict:
        """逐项对账三个来源：候选清单（decision）、执行结果（state）与库内实际数据。"""
        principal.require("retention.read")
        batch = self._require_batch(batch_id)
        items = [dict(row) for row in self.connection.execute(
            "SELECT * FROM cleanup_items WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()]
        audit_rows = [dict(row) for row in self.connection.execute(
            "SELECT id,action,outcome,metadata_json,created_at FROM audit_events "
            "WHERE resource_type='cleanup_batch' AND resource_id=? ORDER BY id",
            (str(batch_id),),
        ).fetchall()]
        audit_actions = [row["action"] for row in audit_rows]
        lines: list[dict] = []
        mismatches = 0
        for item in items:
            exists = self._resource_exists(item["resource_type"], item["resource_id"])
            state = item["state"]
            # 清单候选为 delete 的项才可能被删除；retain 项必须仍在库中
            if state == "deleted":
                store_ok = not exists
                source_ok = item["decision"] == "delete"
            elif state in {"retained", "pending"}:
                store_ok = exists
                source_ok = True
            else:  # skipped：批次取消或记录此前已不存在
                store_ok = True
                source_ok = True
            consistent = store_ok and source_ok
            if not consistent:
                mismatches += 1
            lines.append({
                "resource_type": item["resource_type"],
                "resource_id": item["resource_id"],
                "candidate_decision": item["decision"],
                "execution_state": state,
                "block_reason": item["block_reason"],
                "record_still_exists": exists,
                "consistent": consistent,
            })
        lifecycle_ok = "retention.batch.candidates" in audit_actions
        if batch["status"] in {"confirmed", "executing", "completed"}:
            lifecycle_ok = lifecycle_ok and "retention.batch.confirm" in audit_actions
        if batch["status"] == "completed":
            lifecycle_ok = lifecycle_ok and any(
                action == "retention.batch.execute" for action in audit_actions
            )
        return {
            "batch_id": batch_id,
            "batch_status": batch["status"],
            "summary": self._batch_summary(batch_id),
            "boundary_item_id": batch["boundary_item_id"],
            "items": lines,
            "audit_trail": audit_rows,
            "audit_lifecycle_complete": lifecycle_ok,
            "mismatch_count": mismatches,
            "balanced": mismatches == 0 and lifecycle_ok,
        }

    # ------------------------------------------------------------- 内部辅助

    def _evaluate(
        self,
        row: ResourceRow,
        policies: dict[tuple, dict],
        *,
        moment: str,
        ignore_references: set[tuple[str, int]] | None = None,
    ) -> dict:
        """评估一条记录当前是否可清理。

        冻结与有效业务引用始终按实时状态判定（安全一票否决）；
        ``policies`` 由调用方决定取“当前策略”（解释、候选生成）还是
        “批次确认时快照”（批次执行），从而策略修改不追溯改变已确认批次。
        ``ignore_references`` 中的关联记录在候选生成时将与本记录一并删除，
        不再算作有效引用。
        """
        reasons: list[str] = []
        referenced_by: list[str] = []

        freeze = self.active_freeze_on(row.resource_type, row.resource_id, moment=moment)
        if freeze is not None:
            via = f"（经由 {freeze['propagated_via']} 链冻结传递）" if freeze.get("propagated_via") else ""
            reasons.append(f"处于有效冻结中，至 {freeze['expires_at']} 到期{via}：{freeze['reason']}")

        active_statuses = ACTIVE_BUSINESS_STATUSES.get(row.resource_type, set())
        if row.status in active_statuses:
            reasons.append(f"业务状态为“{row.status}”，仍在有效办理中")

        for ref_type, ref_id, ref_label in self._references(row.resource_type, row.resource_id):
            if ignore_references and (ref_type, ref_id) in ignore_references:
                continue
            referenced_by.append(f"{ref_type}:{ref_id}")
            reasons.append(f"仍被有效业务引用：{ref_label}（{ref_type}:{ref_id}）")

        policy = policies.get((row.resource_type, row.status)) or policies.get((row.resource_type, "*"))
        retain_days = policy["retain_days"] if policy else None
        eligibility_time = None
        if policy is None:
            reasons.append("未配置启用的保留策略，默认保留")
        elif not row.anchor_time:
            reasons.append("缺少业务时间锚点，默认保留")
        elif retain_days is None:
            reasons.append(f"策略要求永久保留（{policy['status']}）")
        else:
            from datetime import timedelta

            from app.core.clock import from_storage

            anchor = from_storage(row.anchor_time)
            eligibility = anchor + timedelta(days=int(retain_days))
            eligibility_time = to_storage(eligibility)
            if moment < eligibility_time:
                reasons.append(f"保留期 {retain_days} 天未满，最早可清理时间 {eligibility_time}")

        return {
            "deletable": not reasons,
            "reasons": reasons,
            "policy": {
                "resource_type": policy["resource_type"],
                "status": policy["status"],
                "retain_days": policy["retain_days"],
            } if policy else None,
            "retain_days": retain_days,
            "eligibility_time": eligibility_time,
            "referenced_by": referenced_by,
        }

    def _references(self, resource_type: str, resource_id: int) -> list[tuple[str, int, str]]:
        """返回仍指向该记录的有效业务引用。"""
        refs: list[tuple[str, int, str]] = []
        conn = self.connection
        if resource_type == "residents":
            row = conn.execute(
                "SELECT id,status,title FROM affairs WHERE applicant_id=? ORDER BY id LIMIT 5",
                (resource_id,),
            ).fetchall()
            for r in row:
                if r["status"] in ("待受理", "办理中"):
                    refs.append(("affairs", r["id"], f"未结事务《{r['title']}》状态{r['status']}"))
                else:
                    refs.append(("affairs", r["id"], f"关联事务《{r['title']}》（{r['status']}）仍引用该居民档案"))
        return refs

    def _collect_chain(self, resource_type: str, resource_id: int) -> list[tuple[str, int]]:
        """链冻结覆盖范围：节点本身及其全部上下游关联数据。"""
        conn = self.connection
        targets: list[tuple[str, int]] = [(resource_type, resource_id)]
        if resource_type == "residents":
            for r in conn.execute("SELECT id FROM affairs WHERE applicant_id=?", (resource_id,)).fetchall():
                targets.append(("affairs", int(r["id"])))
        elif resource_type == "affairs":
            row = conn.execute("SELECT applicant_id FROM affairs WHERE id=?", (resource_id,)).fetchone()
            if row:
                resident_id = int(row["applicant_id"])
                targets.append(("residents", resident_id))
                for r in conn.execute("SELECT id FROM affairs WHERE applicant_id=?", (resident_id,)).fetchall():
                    targets.append(("affairs", int(r["id"])))
        # 去重并保持顺序
        seen: set[tuple[str, int]] = set()
        unique: list[tuple[str, int]] = []
        for target in targets:
            if target not in seen:
                seen.add(target)
                unique.append(target)
        return unique

    def _chain_ancestors(self, resource_type: str, resource_id: int) -> list[tuple[str, int]]:
        """返回能够通过 chain 冻结覆盖本记录的祖先节点。"""
        conn = self.connection
        if resource_type == "affairs":
            row = conn.execute("SELECT applicant_id FROM affairs WHERE id=?", (resource_id,)).fetchone()
            if row:
                return [("residents", int(row["applicant_id"]))]
        return []

    def _scan_resource(self, resource_type: str) -> list[ResourceRow]:
        if resource_type == "residents":
            rows = self.connection.execute(
                "SELECT id,NULL AS status,created_at,name FROM residents ORDER BY id"
            ).fetchall()
            return [ResourceRow("residents", int(r["id"]), None, r["created_at"], f"居民 {r['name']}") for r in rows]
        if resource_type == "affairs":
            rows = self.connection.execute(
                "SELECT id,status,updated_at,title FROM affairs ORDER BY id"
            ).fetchall()
            return [ResourceRow("affairs", int(r["id"]), r["status"], r["updated_at"], f"事务《{r['title']}》") for r in rows]
        if resource_type == "petitions":
            rows = self.connection.execute(
                "SELECT id,status,updated_at,type,target FROM petitions ORDER BY id"
            ).fetchall()
            return [ResourceRow("petitions", int(r["id"]), r["status"], r["updated_at"],
                                f"{r['type']}：{r['target']}") for r in rows]
        if resource_type == "announcements":
            rows = self.connection.execute(
                "SELECT id,NULL AS status,created_at,title FROM announcements ORDER BY id"
            ).fetchall()
            return [ResourceRow("announcements", int(r["id"]), None, r["created_at"],
                                f"公告《{r['title']}》") for r in rows]
        raise ValidationError(f"不支持的资源类型：{resource_type}")

    def _fetch_resource_row(self, resource_type: str, resource_id: int) -> ResourceRow | None:
        conn = self.connection
        if resource_type == "residents":
            r = conn.execute("SELECT id,created_at,name FROM residents WHERE id=?", (resource_id,)).fetchone()
            return None if r is None else ResourceRow("residents", int(r["id"]), None, r["created_at"], f"居民 {r['name']}")
        if resource_type == "affairs":
            r = conn.execute("SELECT id,status,updated_at,title FROM affairs WHERE id=?", (resource_id,)).fetchone()
            return None if r is None else ResourceRow("affairs", int(r["id"]), r["status"], r["updated_at"], f"事务《{r['title']}》")
        if resource_type == "petitions":
            r = conn.execute("SELECT id,status,updated_at,type,target FROM petitions WHERE id=?", (resource_id,)).fetchone()
            return None if r is None else ResourceRow("petitions", int(r["id"]), r["status"], r["updated_at"], f"{r['type']}：{r['target']}")
        if resource_type == "announcements":
            r = conn.execute("SELECT id,created_at,title FROM announcements WHERE id=?", (resource_id,)).fetchone()
            return None if r is None else ResourceRow("announcements", int(r["id"]), None, r["created_at"], f"公告《{r['title']}》")
        raise ValidationError(f"不支持的资源类型：{resource_type}")

    def _delete_resource(self, resource_type: str, resource_id: int) -> None:
        if resource_type == "petitions":
            # 子表带 ON DELETE CASCADE，但连接级外键已开启；显式删除流转与催办记录保证留痕边界清晰
            self.connection.execute("DELETE FROM petition_flow_records WHERE petition_id=?", (resource_id,))
            self.connection.execute("DELETE FROM petition_urges WHERE petition_id=?", (resource_id,))
        self.connection.execute(f"DELETE FROM {resource_type} WHERE id=?", (resource_id,))

    def _resource_exists(self, resource_type: str, resource_id: int) -> bool:
        return self.connection.execute(
            f"SELECT 1 FROM {resource_type} WHERE id=?", (resource_id,)
        ).fetchone() is not None

    def _active_policy_map(self) -> dict[tuple, dict]:
        rows = self.connection.execute(
            "SELECT * FROM retention_policies WHERE is_active=1"
        ).fetchall()
        return {(r["resource_type"], r["status"]): dict(r) for r in rows}

    @staticmethod
    def _policy_map_from_snapshot(snapshot_json: str | None) -> dict[tuple, dict]:
        if not snapshot_json:
            return {}
        try:
            rows = json.loads(snapshot_json)
        except (ValueError, TypeError):
            return {}
        return {(r["resource_type"], r["status"]): r for r in rows if r.get("is_active", 1)}

    def _require_resource_exists(self, resource_type: str, resource_id: int) -> None:
        self._validate_target(resource_type, "*")
        if not self._resource_exists(resource_type, resource_id):
            raise NotFoundError(f"{resource_type}:{resource_id} 不存在")

    def _validate_target(self, resource_type: str, status: str) -> None:
        if resource_type not in RESOURCE_TYPES:
            raise ValidationError(f"不支持的资源类型：{resource_type}，可选 {list(RESOURCE_TYPES)}")
        allowed = RESOURCE_STATUSES[resource_type]
        if status != "*" and (allowed is None or status not in allowed):
            if allowed is None:
                raise ValidationError(f"{resource_type} 没有业务状态，只能使用 *")
            raise ValidationError(f"未知状态：{status}")

    def _validate_resource_types(self, resource_types: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in resource_types:
            if item not in RESOURCE_TYPES:
                raise ValidationError(f"不支持的资源类型：{item}")
            if item not in normalized:
                normalized.append(item)
        return normalized

    def _require_freeze(self, freeze_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM retention_freezes WHERE id=?", (freeze_id,)).fetchone()
        if row is None:
            raise NotFoundError("冻结记录不存在")
        return dict(row)

    def _require_batch(self, batch_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM cleanup_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("清理批次不存在")
        return dict(row)

    def _batch_summary(self, batch_id: int) -> dict:
        rows = self.connection.execute(
            "SELECT decision,state,COUNT(*) AS c FROM cleanup_items WHERE batch_id=? GROUP BY decision,state",
            (batch_id,),
        ).fetchall()
        summary = {
            "total": 0,
            "candidate_delete": 0,
            "candidate_retain": 0,
            "deleted": 0,
            "retained": 0,
            "pending": 0,
            "skipped": 0,
        }
        for row in rows:
            c = int(row["c"])
            summary["total"] += c
            summary[row["state"]] += c
            if row["decision"] == "delete":
                summary["candidate_delete"] += c
            else:
                summary["candidate_retain"] += c
        return summary

    def _serialize_targets(self, targets: list[tuple[str, int]]) -> list[str]:
        return [f"{kind}:{rid}" for kind, rid in targets]

    def _immediate(self):
        from app.database import transaction

        return transaction(immediate=True)
