from __future__ import annotations

import pytest

from app.database import get_connection


def create_resident(client, id_card: str) -> int:
    response = client.post(
        "/residents",
        json={
            "name": "张三",
            "id_card": id_card,
            "gender": "男",
            "birth_date": "1990-01-01",
            "address": "幸福路一号",
            "village": "幸福村",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_department(client) -> int:
    response = client.post("/departments", json={"name": "综合服务中心", "manager": "李主任", "phone": "010-12345678"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_affair(client, resident_id: int, department_id: int, final_status: str) -> int:
    response = client.post("/affairs", json={"title": "社保材料补录", "category": "社保", "applicant_id": resident_id})
    assert response.status_code == 201, response.text
    affair_id = response.json()["id"]
    if final_status != "待受理":
        response = client.put(
            f"/affairs/{affair_id}/process",
            json={"status": "办理中", "department_id": department_id, "handler": "王经办"},
        )
        assert response.status_code == 200, response.text
    if final_status == "已办结":
        response = client.put(
            f"/affairs/{affair_id}/process",
            json={"status": "已办结", "department_id": department_id, "handler": "王经办", "result": "补录完成"},
        )
        assert response.status_code == 200, response.text
    return affair_id


def create_completed_petition(client, department_id: int) -> int:
    response = client.post("/petitions", json={"type": "意见建议", "target": "村道照明", "content": "建议增设照明"})
    assert response.status_code == 201, response.text
    petition_id = response.json()["id"]
    assert client.post(f"/petitions/{petition_id}/receive").status_code == 200
    assert client.post(f"/petitions/{petition_id}/assign", json={"department_id": department_id, "deadline_days": 5}).status_code == 200
    assert client.post(f"/petitions/{petition_id}/process", json={"result": "已安排施工"}).status_code == 200
    assert client.post(f"/petitions/{petition_id}/review", json={"passed": True, "review_opinion": "通过"}).status_code == 200
    return petition_id


def age(table: str, record_id: int, days: int, column: str = "updated_at") -> None:
    get_connection().execute(f"UPDATE {table} SET {column}=datetime('now', ?) WHERE id=?", (f"-{days} days", record_id))


def age_all(table: str, days: int, column: str = "created_at") -> None:
    get_connection().execute(f"UPDATE {table} SET {column}=datetime('now', ?)", (f"-{days} days",))


def put_policy(client, admin, resource_type: str, retention_days: int, status_filter: str | None = None) -> dict:
    payload: dict = {"resource_type": resource_type, "retention_days": retention_days}
    if status_filter is not None:
        payload["status_filter"] = status_filter
    response = client.put("/api/retention/policies", headers=admin["headers"], json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def generate_plan(client, admin, payload: dict | None = None) -> dict:
    response = client.post("/api/retention/plans", headers=admin["headers"], json=payload or {})
    assert response.status_code == 201, response.text
    return response.json()


def confirm_plan(client, admin, plan_id: int) -> None:
    response = client.post(f"/api/retention/plans/{plan_id}/confirm", headers=admin["headers"])
    assert response.status_code == 200, response.text


def execute_plan(client, admin, plan_id: int, payload: dict | None = None) -> dict:
    response = client.post(f"/api/retention/plans/{plan_id}/execute", headers=admin["headers"], json=payload or {})
    assert response.status_code == 200, response.text
    return response.json()


def create_hold(client, admin, resource_type: str, resource_id: int, scope: str = "record", reason: str = "审计调查") -> dict:
    response = client.post(
        "/api/retention/holds",
        headers=admin["headers"],
        json={
            "resource_type": resource_type,
            "resource_id": resource_id,
            "scope": scope,
            "reason": reason,
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_policy_upsert_list_and_validation(client, admin):
    policy = put_policy(client, admin, "affair", 365, "已办结")
    assert policy["resource_type"] == "affair"
    assert policy["status_filter"] == "已办结"

    updated = put_policy(client, admin, "affair", 180, "已办结")
    assert updated["id"] == policy["id"]
    assert updated["retention_days"] == 180

    response = client.put(
        "/api/retention/policies",
        headers=admin["headers"],
        json={"resource_type": "resident", "status_filter": "已办结", "retention_days": 30},
    )
    assert response.status_code == 422

    response = client.put(
        "/api/retention/policies",
        headers=admin["headers"],
        json={"resource_type": "affair", "status_filter": "不存在的状态", "retention_days": 30},
    )
    assert response.status_code == 422

    policies = client.get("/api/retention/policies", headers=admin["headers"]).json()["data"]
    assert len(policies) == 1

    response = client.delete(f"/api/retention/policies/{policy['id']}", headers=admin["headers"])
    assert response.status_code == 200
    assert client.get("/api/retention/policies", headers=admin["headers"]).json()["data"] == []


def test_hold_lifecycle_and_validation(client, admin):
    resident_id = create_resident(client, "110101199001010001")

    response = client.post(
        "/api/retention/holds",
        headers=admin["headers"],
        json={"resource_type": "resident", "resource_id": resident_id, "reason": "审计调查", "expires_at": "2020-01-01T00:00:00+00:00"},
    )
    assert response.status_code == 422

    response = client.post(
        "/api/retention/holds",
        headers=admin["headers"],
        json={"resource_type": "resident", "resource_id": 9999, "reason": "审计调查", "expires_at": "2099-01-01T00:00:00+00:00"},
    )
    assert response.status_code == 404

    hold = create_hold(client, admin, "resident", resident_id, reason="配合审计调查")
    assert hold["scope"] == "record"
    assert hold["reason"] == "配合审计调查"

    holds = client.get("/api/retention/holds", headers=admin["headers"]).json()["data"]
    assert [item["id"] for item in holds] == [hold["id"]]

    response = client.post(f"/api/retention/holds/{hold['id']}/release", headers=admin["headers"], json={"reason": "调查结束"})
    assert response.status_code == 200
    assert response.json()["released_at"] is not None
    assert client.get("/api/retention/holds?active_only=true", headers=admin["headers"]).json()["data"] == []

    response = client.post(f"/api/retention/holds/{hold['id']}/release", headers=admin["headers"], json={"reason": "重复解除"})
    assert response.status_code == 409


def test_expired_hold_does_not_protect(client, admin):
    resident_id = create_resident(client, "110101199001010002")
    hold = create_hold(client, admin, "resident", resident_id)
    get_connection().execute("UPDATE retention_holds SET expires_at=datetime('now','-1 day') WHERE id=?", (hold["id"],))
    assert client.get("/api/retention/holds?active_only=true", headers=admin["headers"]).json()["data"] == []

    put_policy(client, admin, "resident", 365)
    age("residents", resident_id, 400)
    plan = generate_plan(client, admin)
    assert plan["totals"]["total"] == 1


def test_generate_plan_requires_enabled_policy(client, admin):
    response = client.post("/api/retention/plans", headers=admin["headers"], json={})
    assert response.status_code == 422


def test_cleanup_lifecycle_with_freeze_and_references(client, admin):
    department_id = create_department(client)
    put_policy(client, admin, "affair", 365, "已办结")
    put_policy(client, admin, "petition", 365, "已办结")
    put_policy(client, admin, "resident", 365)

    resident_free = create_resident(client, "110101199001010011")
    resident_done = create_resident(client, "110101199001010012")
    affair_done = create_affair(client, resident_done, department_id, "已办结")
    resident_busy = create_resident(client, "110101199001010013")
    affair_active = create_affair(client, resident_busy, department_id, "办理中")
    resident_holder = create_resident(client, "110101199001010014")
    affair_frozen = create_affair(client, resident_holder, department_id, "已办结")
    resident_young = create_resident(client, "110101199001010015")
    petition_done = create_completed_petition(client, department_id)
    petition_review = create_completed_petition(client, department_id)
    assert client.post(f"/petitions/{petition_review}/reapply-review", json={"reason": "不服结果"}).status_code == 200

    for resident_id in (resident_free, resident_done, resident_busy, resident_holder):
        age("residents", resident_id, 400)
    for affair_id in (affair_done, affair_active, affair_frozen):
        age("affairs", affair_id, 400)
    age("petitions", petition_done, 400)
    age("petitions", petition_review, 400)

    plan = generate_plan(client, admin, {"notes": "年度清理"})
    plan_id = plan["plan"]["id"]
    assert plan["totals"]["total"] == 7
    assert plan["totals"]["pending"] == 7

    # 生成清单后才设置的冻结，执行时仍然生效
    create_hold(client, admin, "affair", affair_frozen, reason="群众投诉复核")

    # 未确认的清单不能执行
    response = client.post(f"/api/retention/plans/{plan_id}/execute", headers=admin["headers"], json={})
    assert response.status_code == 409

    confirm_plan(client, admin, plan_id)
    result = execute_plan(client, admin, plan_id, {"batch_size": 2})
    assert result["plan"]["status"] == "completed"
    assert len(result["batches"]) == 4
    totals = result["totals"]
    assert totals["deleted"] == 4
    assert totals["skipped_frozen"] == 1
    assert totals["skipped_referenced"] == 2
    assert totals["pending"] == 0

    # 实际记录核验：办结事务、办结信访、无引用居民被清理
    assert client.get(f"/residents/{resident_free}").status_code == 404
    assert client.get(f"/residents/{resident_done}").status_code == 404
    assert client.get(f"/affairs/{affair_done}").status_code == 404
    assert client.get(f"/petitions/{petition_done}").status_code == 404
    # 被冻结、被引用、复查中、未达到期限的记录全部保留
    assert client.get(f"/affairs/{affair_frozen}").status_code == 200
    assert client.get(f"/residents/{resident_busy}").status_code == 200
    assert client.get(f"/residents/{resident_holder}").status_code == 200
    assert client.get(f"/residents/{resident_young}").status_code == 200
    assert client.get(f"/petitions/{petition_review}").status_code == 200

    # 重复执行已完成清单：不产生新批次、计数不变
    again = execute_plan(client, admin, plan_id)
    assert again["batches"] == []
    assert again["totals"] == totals

    # 对账：清单、批次、审计记录逐项一致
    reconcile = client.get(f"/api/retention/plans/{plan_id}/reconcile", headers=admin["headers"])
    assert reconcile.status_code == 200
    report = reconcile.json()
    assert report["ok"] is True
    assert report["discrepancies"] == []
    assert len(report["batches"]) == 4
    assert all(batch["audit_events"] == 1 for batch in report["batches"])
    assert sum(batch["deleted"] for batch in report["batches"]) == 4

    # 解释：被引用保留
    explain = client.get(f"/api/retention/explain?resource_type=resident&resource_id={resident_busy}", headers=admin["headers"])
    assert explain.status_code == 200
    assert explain.json()["verdict"] == "retained_reference"
    assert "引用" in explain.json()["verdict_text"]

    # 解释：被冻结保留
    explain = client.get(f"/api/retention/explain?resource_type=affair&resource_id={affair_frozen}", headers=admin["headers"])
    assert explain.json()["verdict"] == "retained_hold"
    assert explain.json()["holds"][0]["reason"] == "群众投诉复核"

    # 解释：已被清理
    explain = client.get(f"/api/retention/explain?resource_type=resident&resource_id={resident_free}", headers=admin["headers"])
    assert explain.json()["verdict"] == "absent"
    assert explain.json()["plan_history"][0]["decision"] == "deleted"

    # 解释：未达到保留期限
    explain = client.get(f"/api/retention/explain?resource_type=resident&resource_id={resident_young}", headers=admin["headers"])
    assert explain.json()["verdict"] == "retained_policy"


def test_confirmed_plan_not_affected_by_policy_change(client, admin):
    department_id = create_department(client)
    put_policy(client, admin, "affair", 30, "已办结")
    resident_id = create_resident(client, "110101199001010021")
    affair_id = create_affair(client, resident_id, department_id, "已办结")
    age("affairs", affair_id, 40)

    plan = generate_plan(client, admin)
    plan_id = plan["plan"]["id"]
    assert plan["totals"]["total"] == 1
    confirm_plan(client, admin, plan_id)

    # 确认后修改策略不影响已确认清单
    put_policy(client, admin, "affair", 9999, "已办结")
    result = execute_plan(client, admin, plan_id)
    assert result["totals"]["deleted"] == 1
    assert client.get(f"/affairs/{affair_id}").status_code == 404


def test_interrupted_execution_resumes_from_committed_boundary(client, admin):
    department_id = create_department(client)
    put_policy(client, admin, "affair", 365, "已办结")
    put_policy(client, admin, "resident", 365)
    resident_id = create_resident(client, "110101199001010031")
    affair_ids = [create_affair(client, resident_id, department_id, "已办结") for _ in range(3)]
    age("residents", resident_id, 400)
    for affair_id in affair_ids:
        age("affairs", affair_id, 400)

    plan = generate_plan(client, admin)
    plan_id = plan["plan"]["id"]
    assert plan["totals"]["total"] == 4
    confirm_plan(client, admin, plan_id)

    # 只执行一个批次后中断（模拟任务中断）
    first = execute_plan(client, admin, plan_id, {"batch_size": 2, "max_batches": 1})
    assert len(first["batches"]) == 1
    assert first["plan"]["status"] == "executing"
    assert first["totals"]["pending"] == 2

    # 从已提交边界继续，最终完成
    second = execute_plan(client, admin, plan_id)
    assert second["plan"]["status"] == "completed"
    assert second["totals"]["deleted"] == 4
    assert client.get(f"/residents/{resident_id}").status_code == 404

    reconcile = client.get(f"/api/retention/plans/{plan_id}/reconcile", headers=admin["headers"]).json()
    assert reconcile["ok"] is True
    assert [batch["batch_no"] for batch in reconcile["batches"]] == [1, 2]


def test_chain_hold_protects_related_records(client, admin):
    department_id = create_department(client)
    put_policy(client, admin, "affair", 365, "已办结")
    put_policy(client, admin, "resident", 365)
    resident_chain = create_resident(client, "110101199001010041")
    affair_chain = create_affair(client, resident_chain, department_id, "已办结")
    resident_plain = create_resident(client, "110101199001010042")
    age("residents", resident_chain, 400)
    age("affairs", affair_chain, 400)
    age("residents", resident_plain, 400)

    hold = create_hold(client, admin, "resident", resident_chain, scope="chain", reason="审计调查取证")

    # 关联链上的居民与事务都不进入候选清单
    plan = generate_plan(client, admin)
    plan_id = plan["plan"]["id"]
    assert plan["totals"]["total"] == 1
    assert plan["items"][0]["resource_id"] == resident_plain
    confirm_plan(client, admin, plan_id)
    execute_plan(client, admin, plan_id)
    assert client.get(f"/residents/{resident_plain}").status_code == 404
    assert client.get(f"/residents/{resident_chain}").status_code == 200
    assert client.get(f"/affairs/{affair_chain}").status_code == 200

    explain = client.get(f"/api/retention/explain?resource_type=affair&resource_id={affair_chain}", headers=admin["headers"]).json()
    assert explain["verdict"] == "retained_hold"
    assert explain["holds"][0]["via"] == f"chain:resident/{resident_chain}"

    # 解除冻结后，新一轮清单可以清理
    client.post(f"/api/retention/holds/{hold['id']}/release", headers=admin["headers"], json={"reason": "调查结束"})
    plan = generate_plan(client, admin)
    plan_id = plan["plan"]["id"]
    assert plan["totals"]["total"] == 2
    confirm_plan(client, admin, plan_id)
    execute_plan(client, admin, plan_id)
    assert client.get(f"/residents/{resident_chain}").status_code == 404
    assert client.get(f"/affairs/{affair_chain}").status_code == 404


def test_chain_hold_protects_audit_evidence(client, admin):
    from app.services.audit import AuditContext, AuditService

    department_id = create_department(client)
    petition_id = create_completed_petition(client, department_id)
    AuditService(get_connection()).record(
        AuditContext(None, "信访办"),
        action="petition.transition",
        resource_type="petition",
        resource_id=petition_id,
        after={"status": "复查中"},
    )
    event_id = get_connection().execute(
        "SELECT id FROM audit_events WHERE resource_type='petition' AND resource_id=?", (str(petition_id),)
    ).fetchone()[0]

    put_policy(client, admin, "audit_event", 365)
    age_all("audit_events", 400)
    create_hold(client, admin, "petition", petition_id, scope="chain", reason="复查期间保留证据")

    plan = generate_plan(client, admin, {"resource_types": ["audit_event"]})
    plan_id = plan["plan"]["id"]
    candidate_ids = {item["resource_id"] for item in plan["items"]}
    assert event_id not in candidate_ids
    assert plan["totals"]["total"] >= 1
    confirm_plan(client, admin, plan_id)
    execute_plan(client, admin, plan_id)

    remaining = get_connection().execute("SELECT COUNT(*) FROM audit_events WHERE id=?", (event_id,)).fetchone()[0]
    assert remaining == 1
    explain = client.get(f"/api/retention/explain?resource_type=audit_event&resource_id={event_id}", headers=admin["headers"]).json()
    assert explain["verdict"] == "retained_hold"
    assert explain["holds"][0]["via"] == f"chain:petition/{petition_id}"


def test_pending_items_not_double_counted_across_plans(client, admin):
    put_policy(client, admin, "resident", 365)
    resident_id = create_resident(client, "110101199001010051")
    age("residents", resident_id, 400)

    first = generate_plan(client, admin)
    assert first["totals"]["total"] == 1
    # 已有待处理清单时，重复生成不会重复计数
    second = generate_plan(client, admin)
    assert second["totals"]["total"] == 0
    # 取消后重新生成可以再次纳入
    response = client.post(f"/api/retention/plans/{first['plan']['id']}/cancel", headers=admin["headers"])
    assert response.status_code == 200
    third = generate_plan(client, admin)
    assert third["totals"]["total"] == 1


def test_failed_batch_rolls_back_and_resumes(client, admin):
    from app.core.security import Principal
    from app.services.retention import RetentionService

    department_id = create_department(client)
    put_policy(client, admin, "affair", 365, "已办结")
    resident_id = create_resident(client, "110101199001010071")
    affair_ids = [create_affair(client, resident_id, department_id, "已办结") for _ in range(3)]
    for affair_id in affair_ids:
        age("affairs", affair_id, 400)

    plan = generate_plan(client, admin)
    plan_id = plan["plan"]["id"]
    assert plan["totals"]["total"] == 3
    confirm_plan(client, admin, plan_id)

    principal = Principal(1, "admin", "管理员", None, frozenset({"*"}), 1)
    service = RetentionService(get_connection())
    original = service._process_item
    calls = {"count": 0}

    def flaky(item, protected):
        calls["count"] += 1
        if calls["count"] == 3:
            raise RuntimeError("模拟执行中断")
        return original(item, protected)

    service._process_item = flaky
    with pytest.raises(RuntimeError):
        service.execute_plan(principal, plan_id, batch_size=5)

    # 失败批次整体回滚：没有条目被标记、没有批次记录，清单标记为失败
    connection = get_connection()
    assert connection.execute("SELECT status FROM cleanup_plans WHERE id=?", (plan_id,)).fetchone()[0] == "failed"
    assert connection.execute(
        "SELECT COUNT(*) FROM cleanup_plan_items WHERE plan_id=? AND status='pending'", (plan_id,)
    ).fetchone()[0] == 3
    assert connection.execute("SELECT COUNT(*) FROM cleanup_batches WHERE plan_id=?", (plan_id,)).fetchone()[0] == 0
    assert all(client.get(f"/affairs/{affair_id}").status_code == 200 for affair_id in affair_ids)

    # 从已提交边界继续执行，最终完成且不重复计数
    recovered = RetentionService(get_connection()).execute_plan(principal, plan_id)
    assert recovered["plan"]["status"] == "completed"
    assert recovered["totals"]["deleted"] == 3
    assert all(client.get(f"/affairs/{affair_id}").status_code == 404 for affair_id in affair_ids)

    reconcile = client.get(f"/api/retention/plans/{plan_id}/reconcile", headers=admin["headers"]).json()
    assert reconcile["ok"] is True


def test_announcement_cleanup(client, admin):
    put_policy(client, admin, "announcement", 365)
    response = client.post(
        "/announcements",
        json={"title": "过期公示", "content": "正文", "category": "公示", "publisher": "办公室"},
    )
    assert response.status_code == 201
    announcement_id = response.json()["id"]
    age("announcements", announcement_id, 400, column="created_at")

    plan = generate_plan(client, admin)
    plan_id = plan["plan"]["id"]
    assert plan["totals"]["total"] == 1
    confirm_plan(client, admin, plan_id)
    result = execute_plan(client, admin, plan_id)
    assert result["totals"]["deleted"] == 1
    assert client.get("/announcements").json()["data"] == []


def test_retention_permissions_are_enforced(client, admin):
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "retention.operator", "name": "清理经办", "permission_codes": ["retention.read", "retention.write"]},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "retention.op", "password": "Clerk!23456", "display_name": "清理经办员", "role_codes": ["retention.operator"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": "retention.op", "password": "Clerk!23456", "client_label": "tests"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    put_policy(client, admin, "resident", 365)
    resident_id = create_resident(client, "110101199001010061")
    age("residents", resident_id, 400)

    # 经办可以生成清单，但不能确认、不能执行
    response = client.post("/api/retention/plans", headers=headers, json={})
    assert response.status_code == 201
    plan_id = response.json()["plan"]["id"]
    assert client.post(f"/api/retention/plans/{plan_id}/confirm", headers=headers).status_code == 403
    confirm_plan(client, admin, plan_id)
    assert client.post(f"/api/retention/plans/{plan_id}/execute", headers=headers, json={}).status_code == 403
    assert execute_plan(client, admin, plan_id)["totals"]["deleted"] == 1

    # 没有任何 retention 权限的账号不能查看
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "plain.reader", "name": "普通查看", "permission_codes": ["residents.read"]},
    )
    assert role.status_code == 201
    client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "plain.reader", "password": "Clerk!23456", "display_name": "普通查看员", "role_codes": ["plain.reader"]},
    )
    login = client.post("/api/auth/login", json={"username": "plain.reader", "password": "Clerk!23456", "client_label": "tests"})
    plain_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/api/retention/policies", headers=plain_headers).status_code == 403
    assert client.get(f"/api/retention/plans/{plan_id}/reconcile", headers=plain_headers).status_code == 403
