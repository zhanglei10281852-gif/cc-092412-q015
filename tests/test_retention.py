from __future__ import annotations

from app.database import get_connection


# --------------------------------------------------------------------- 辅助


def backdate(table: str, entity_id: int, *, column: str, days: int) -> None:
    conn = get_connection()
    conn.execute(
        f"UPDATE {table} SET {column}=datetime('now', ?) WHERE id=?",
        (f"-{days} days", entity_id),
    )
    conn.commit()


def expire_freeze(freeze_id: int, *, days: int = 1) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE retention_freezes SET created_at=datetime('now',?),expires_at=datetime('now',?) WHERE id=?",
        (f"-{days + 1} days", f"-{days} days", freeze_id),
    )
    conn.commit()


def make_resident(client, name: str = "张三", id_card: str = "110101199001011234") -> int:
    response = client.post(
        "/residents",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1990-01-01",
              "phone": "13800000000", "address": "幸福路一号", "village": "幸福村"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_department(client) -> int:
    response = client.post("/departments", json={"name": "综合服务中心", "manager": "李主任", "phone": "010-12345678"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_completed_affair(client, resident_id: int, department_id: int, title: str = "社保补录") -> int:
    created = client.post("/affairs", json={"title": title, "category": "社保", "applicant_id": resident_id})
    assert created.status_code == 201, created.text
    affair_id = created.json()["id"]
    assert client.put(f"/affairs/{affair_id}/process",
                      json={"status": "办理中", "department_id": department_id, "handler": "王经办"}).status_code == 200
    assert client.put(f"/affairs/{affair_id}/process",
                      json={"status": "已办结", "department_id": department_id, "handler": "王经办",
                            "result": "办结"}).status_code == 200
    return affair_id


def make_announcement(client, title: str) -> int:
    response = client.post("/announcements",
                           json={"title": title, "content": "正文", "category": "通知",
                                 "publisher": "办公室", "is_pinned": False})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def put_policy(client, headers, resource_type: str, status: str, days, *, is_active: bool = True):
    response = client.put("/api/retention/policies", headers=headers, json={
        "resource_type": resource_type, "status": status, "retain_days": days, "is_active": is_active,
    })
    assert response.status_code == 200, response.text
    return response.json()


def run_full_batch(client, headers, resource_types, *, limit: int = 200, note=""):
    created = client.post("/api/retention/batches/candidates", headers=headers,
                          json={"resource_types": resource_types, "note": note})
    assert created.status_code == 201, created.text
    batch = created.json()
    confirmed = client.post(f"/api/retention/batches/{batch['id']}/confirm", headers=headers,
                            json={"authorization_note": "清理审批单 2026-001，经分管领导授权"})
    assert confirmed.status_code == 200, confirmed.text
    executed = client.post(f"/api/retention/batches/{batch['id']}/execute?limit={limit}", headers=headers)
    assert executed.status_code == 200, executed.text
    return executed.json()


# --------------------------------------------------------------------- 策略


def test_default_policies_seeded(client, admin):
    response = client.get("/api/retention/policies", headers=admin["headers"])
    assert response.status_code == 200
    policies = {(p["resource_type"], p["status"]): p["retain_days"] for p in response.json()}
    assert policies[("affairs", "已办结")] == 1825
    assert policies[("residents", "*")] is None  # 默认永久保留


def test_policy_requires_permission(client):
    assert client.get("/api/retention/policies").status_code == 401


def test_clerk_cannot_configure_or_clean(client, admin):
    created = client.post("/api/users", headers=admin["headers"], json={
        "username": "clerk01", "password": "Clerk!123456", "display_name": "经办员", "role_codes": ["clerk"],
    })
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={
        "username": "clerk01", "password": "Clerk!123456", "client_label": "t",
    })
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.put("/api/retention/policies", headers=headers, json={
        "resource_type": "affairs", "status": "已办结", "retain_days": 1,
    }).status_code == 403
    assert client.post("/api/retention/batches/candidates", headers=headers,
                       json={"resource_types": ["affairs"]}).status_code == 403


def test_policy_rejects_unknown_status(client, admin):
    response = client.put("/api/retention/policies", headers=admin["headers"], json={
        "resource_type": "affairs", "status": "不存在的状态", "retain_days": 1,
    })
    assert response.status_code == 422
    response = client.put("/api/retention/policies", headers=admin["headers"], json={
        "resource_type": "residents", "status": "已办结", "retain_days": 1,
    })
    assert response.status_code == 422


# --------------------------------------------------------------------- 清理主流程


def test_candidate_confirm_execute_deletes_expired(client, admin):
    headers = admin["headers"]
    resident_id = make_resident(client)
    department_id = make_department(client)
    affair_id = make_completed_affair(client, resident_id, department_id)
    backdate("affairs", affair_id, column="updated_at", days=40)
    put_policy(client, headers, "affairs", "已办结", 30)

    batch = run_full_batch(client, headers, ["affairs"])
    assert batch["status"] == "completed"
    assert batch["deleted"] == 1
    assert client.get(f"/affairs/{affair_id}").status_code == 404


def test_unconfirmed_batch_cannot_execute(client, admin):
    created = client.post("/api/retention/batches/candidates", headers=admin["headers"],
                          json={"resource_types": ["affairs"]})
    batch_id = created.json()["id"]
    assert client.post(f"/api/retention/batches/{batch_id}/execute", headers=admin["headers"]).status_code == 409


def test_confirm_requires_authorization_note(client, admin):
    created = client.post("/api/retention/batches/candidates", headers=admin["headers"],
                          json={"resource_types": ["affairs"]})
    batch_id = created.json()["id"]
    response = client.post(f"/api/retention/batches/{batch_id}/confirm", headers=admin["headers"],
                           json={"authorization_note": "x"})
    assert response.status_code == 422


def test_active_business_and_references_never_deleted(client, admin):
    headers = admin["headers"]
    resident_id = make_resident(client, id_card="110101199002021234")
    # 在办（待受理）事务：居民被有效业务引用，事务处于未结状态
    active_affair = client.post("/affairs", json={
        "title": "办理中的事项", "category": "医保", "applicant_id": resident_id,
    }).json()["id"]
    backdate("residents", resident_id, column="created_at", days=400)
    put_policy(client, headers, "residents", "*", 30)
    put_policy(client, headers, "affairs", "待受理", 30)

    batch = run_full_batch(client, headers, ["residents", "affairs"])
    assert batch["deleted"] == 0
    items = client.get(f"/api/retention/batches/{batch['id']}/items", headers=headers).json()["data"]
    by_target = {(i["resource_type"], i["resource_id"]): i for i in items}
    assert by_target[("affairs", active_affair)]["decision"] == "retain"
    assert "待受理" in by_target[("affairs", active_affair)]["block_reason"]
    resident_item = by_target[("residents", resident_id)]
    assert resident_item["decision"] == "retain"
    assert "仍被有效业务引用" in resident_item["block_reason"]
    assert client.get(f"/residents/{resident_id}").status_code == 200


def test_active_petition_never_deleted(client, admin):
    headers = admin["headers"]
    department_id = make_department(client)
    petition_id = client.post("/petitions", json={
        "type": "投诉举报", "target": "村道工程", "content": "投诉质量问题",
    }).json()["id"]
    client.post(f"/petitions/{petition_id}/receive")
    client.post(f"/petitions/{petition_id}/assign", json={"department_id": department_id, "deadline_days": 5})
    client.post(f"/petitions/{petition_id}/process", json={"result": "正在核查"})
    client.post(f"/petitions/{petition_id}/review", json={"passed": True})
    client.post(f"/petitions/{petition_id}/reapply-review", json={"reason": "群众申请复查"})
    backdate("petitions", petition_id, column="updated_at", days=900)
    put_policy(client, headers, "petitions", "复查中", 30)

    batch = run_full_batch(client, headers, ["petitions"])
    assert batch["deleted"] == 0
    assert client.get(f"/petitions/{petition_id}").status_code == 200


def test_resident_and_completed_affair_deleted_together(client, admin):
    headers = admin["headers"]
    resident_id = make_resident(client)
    department_id = make_department(client)
    affair_id = make_completed_affair(client, resident_id, department_id)
    backdate("affairs", affair_id, column="updated_at", days=400)
    backdate("residents", resident_id, column="created_at", days=400)
    put_policy(client, headers, "residents", "*", 30)
    put_policy(client, headers, "affairs", "已办结", 30)

    batch = run_full_batch(client, headers, ["residents", "affairs"])
    assert batch["deleted"] == 2
    assert client.get(f"/affairs/{affair_id}").status_code == 404
    assert client.get(f"/residents/{resident_id}").status_code == 404


# --------------------------------------------------------------------- 冻结


def test_record_freeze_blocks_cleanup(client, admin):
    headers = admin["headers"]
    announcement_id = make_announcement(client, "旧通知")
    backdate("announcements", announcement_id, column="created_at", days=100)
    put_policy(client, headers, "announcements", "*", 30)

    freeze = client.post("/api/retention/freezes", headers=headers, json={
        "resource_type": "announcements", "resource_id": announcement_id,
        "scope": "record", "reason": "审计调查取证需要", "retain_days": 60,
    })
    assert freeze.status_code == 201, freeze.text
    batch = run_full_batch(client, headers, ["announcements"])
    assert batch["deleted"] == 0
    items = client.get(f"/api/retention/batches/{batch['id']}/items", headers=headers).json()["data"]
    assert "冻结" in items[0]["block_reason"]
    assert "审计调查取证需要" in items[0]["block_reason"]
    assert client.get("/announcements").json()["data"][0]["id"] == announcement_id


def test_freeze_applied_after_candidate_still_blocks_execution(client, admin):
    headers = admin["headers"]
    announcement_id = make_announcement(client, "旧通知")
    backdate("announcements", announcement_id, column="created_at", days=100)
    put_policy(client, headers, "announcements", "*", 30)

    created = client.post("/api/retention/batches/candidates", headers=headers,
                          json={"resource_types": ["announcements"]})
    batch = created.json()
    assert batch["candidate_delete"] == 1
    # 复查启动：在确认后、执行前加冻结
    client.post(f"/api/retention/batches/{batch['id']}/confirm", headers=headers,
                json={"authorization_note": "已授权"})
    freeze = client.post("/api/retention/freezes", headers=headers, json={
        "resource_type": "announcements", "resource_id": announcement_id,
        "reason": "突发投诉需保全证据", "retain_days": 30,
    })
    assert freeze.status_code == 201
    executed = client.post(f"/api/retention/batches/{batch['id']}/execute", headers=headers).json()
    assert executed["deleted"] == 0
    assert executed["retained"] == 1
    assert client.get("/announcements").json()["data"][0]["id"] == announcement_id


def test_chain_freeze_propagates_to_affairs(client, admin):
    headers = admin["headers"]
    resident_id = make_resident(client)
    department_id = make_department(client)
    affair_id = make_completed_affair(client, resident_id, department_id)
    backdate("affairs", affair_id, column="updated_at", days=400)
    backdate("residents", resident_id, column="created_at", days=400)
    put_policy(client, headers, "residents", "*", 30)
    put_policy(client, headers, "affairs", "已办结", 30)

    freeze = client.post("/api/retention/freezes", headers=headers, json={
        "resource_type": "residents", "resource_id": resident_id,
        "scope": "chain", "reason": "复查该居民全部历史事项", "retain_days": 90,
    }).json()
    covered = client.get(f"/api/retention/freezes/{freeze['id']}", headers=headers)
    assert covered.status_code == 200

    explanation = client.get(f"/api/retention/explain/affairs/{affair_id}", headers=headers).json()
    assert explanation["verdict"] == "retain"
    assert explanation["active_freeze"]["propagated_via"] == f"residents:{resident_id}"

    batch = run_full_batch(client, headers, ["residents", "affairs"])
    assert batch["deleted"] == 0


def test_freeze_release_allows_cleanup(client, admin):
    headers = admin["headers"]
    announcement_id = make_announcement(client, "旧通知")
    backdate("announcements", announcement_id, column="created_at", days=100)
    put_policy(client, headers, "announcements", "*", 30)
    freeze_id = client.post("/api/retention/freezes", headers=headers, json={
        "resource_type": "announcements", "resource_id": announcement_id,
        "reason": "审计取证", "retain_days": 10,
    }).json()["id"]
    released = client.post(f"/api/retention/freezes/{freeze_id}/release", headers=headers,
                           json={"reason": "审计结束，解除保全"})
    assert released.status_code == 200
    assert released.json()["released_at"]
    batch = run_full_batch(client, headers, ["announcements"])
    assert batch["deleted"] == 1


def test_expired_freeze_no_longer_blocks(client, admin):
    headers = admin["headers"]
    announcement_id = make_announcement(client, "旧通知")
    backdate("announcements", announcement_id, column="created_at", days=100)
    put_policy(client, headers, "announcements", "*", 30)
    freeze_id = client.post("/api/retention/freezes", headers=headers, json={
        "resource_type": "announcements", "resource_id": announcement_id,
        "reason": "审计取证", "retain_days": 1,
    }).json()["id"]
    expire_freeze(freeze_id)
    active = client.get("/api/retention/freezes?active_only=true", headers=headers).json()
    assert all(item["id"] != freeze_id for item in active)
    batch = run_full_batch(client, headers, ["announcements"])
    assert batch["deleted"] == 1


def test_freeze_requires_existing_record(client, admin):
    response = client.post("/api/retention/freezes", headers=admin["headers"], json={
        "resource_type": "announcements", "resource_id": 9999,
        "reason": "不存在的记录", "retain_days": 10,
    })
    assert response.status_code == 404


# ------------------------------------------------------- 断点续跑与重复运行


def test_batched_execution_resumes_from_boundary(client, admin):
    headers = admin["headers"]
    put_policy(client, headers, "announcements", "*", 1)
    ids = [make_announcement(client, f"通知{i}") for i in range(3)]
    for announcement_id in ids:
        backdate("announcements", announcement_id, column="created_at", days=30)

    created = client.post("/api/retention/batches/candidates", headers=headers,
                          json={"resource_types": ["announcements"]})
    batch_id = created.json()["id"]
    assert created.json()["candidate_delete"] == 3
    client.post(f"/api/retention/batches/{batch_id}/confirm", headers=headers, json={"authorization_note": "已授权"})

    first = client.post(f"/api/retention/batches/{batch_id}/execute?limit=1", headers=headers).json()
    assert first["deleted"] == 1
    assert first["status"] == "executing"
    assert first["boundary_item_id"] > 0

    second = client.post(f"/api/retention/batches/{batch_id}/execute?limit=1", headers=headers).json()
    assert second["deleted"] == 2

    third = client.post(f"/api/retention/batches/{batch_id}/execute?limit=1", headers=headers).json()
    assert third["status"] == "completed"
    assert third["deleted"] == 3

    # 任务中断后重复运行：从已提交边界继续，没有剩余项，不重复计数
    fourth = client.post(f"/api/retention/batches/{batch_id}/execute?limit=1", headers=headers).json()
    assert fourth["status"] == "completed"
    assert fourth["deleted"] == 3
    execute_events = [
        e for e in client.get("/api/audit?resource_type=cleanup_batch&size=100", headers=headers).json()["data"]
        if e["action"] == "retention.batch.execute"
    ]
    assert len(execute_events) == 3


# ---------------------------------------------------------- 策略不追溯已确认批次


def test_policy_change_does_not_retroact_confirmed_batch(client, admin):
    headers = admin["headers"]
    announcement_id = make_announcement(client, "旧通知")
    backdate("announcements", announcement_id, column="created_at", days=100)
    put_policy(client, headers, "announcements", "*", 30)

    created = client.post("/api/retention/batches/candidates", headers=headers,
                          json={"resource_types": ["announcements"]})
    batch_id = created.json()["id"]
    client.post(f"/api/retention/batches/{batch_id}/confirm", headers=headers, json={"authorization_note": "已授权"})
    # 确认后改成永久保留：已确认批次仍按确认时快照执行
    put_policy(client, headers, "announcements", "*", None)
    executed = client.post(f"/api/retention/batches/{batch_id}/execute", headers=headers).json()
    assert executed["deleted"] == 1
    # 新批次按新策略生成：没有候选删除项
    later = run_full_batch(client, headers, ["announcements"])
    assert later["candidate_delete"] == 0


# --------------------------------------------------------------------- 取消


def test_cancel_batch_marks_items_skipped(client, admin):
    headers = admin["headers"]
    announcement_id = make_announcement(client, "旧通知")
    backdate("announcements", announcement_id, column="created_at", days=100)
    put_policy(client, headers, "announcements", "*", 30)
    created = client.post("/api/retention/batches/candidates", headers=headers,
                          json={"resource_types": ["announcements"]})
    batch_id = created.json()["id"]
    cancelled = client.post(f"/api/retention/batches/{batch_id}/cancel", headers=headers,
                            json={"reason": "清理计划暂缓"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    items = client.get(f"/api/retention/batches/{batch_id}/items", headers=headers).json()["data"]
    assert items[0]["state"] == "skipped"
    assert client.post(f"/api/retention/batches/{batch_id}/execute", headers=headers).status_code == 409
    assert client.get("/announcements").json()["data"][0]["id"] == announcement_id


# --------------------------------------------------------------- 解释与对账


def test_explain_retention_reasons(client, admin):
    headers = admin["headers"]
    resident_id = make_resident(client)
    active_affair = client.post("/affairs", json={
        "title": "在办", "category": "医保", "applicant_id": resident_id,
    }).json()["id"]
    put_policy(client, headers, "residents", "*", None)

    explanation = client.get(f"/api/retention/explain/residents/{resident_id}", headers=headers).json()
    assert explanation["verdict"] == "retain"
    reasons = "；".join(explanation["reasons"])
    assert "永久保留" in reasons
    assert f"affairs:{active_affair}" in reasons

    missing = client.get("/api/retention/explain/residents/9999", headers=headers).json()
    assert missing["exists"] is False
    assert missing["verdict"] == "absent"


def test_reconcile_balanced_after_execution(client, admin):
    headers = admin["headers"]
    kept = make_announcement(client, "新通知")
    old = make_announcement(client, "旧通知")
    backdate("announcements", old, column="created_at", days=100)
    put_policy(client, headers, "announcements", "*", 30)

    batch = run_full_batch(client, headers, ["announcements"])
    report = client.get(f"/api/retention/batches/{batch['id']}/reconcile", headers=headers).json()
    assert report["balanced"] is True
    assert report["mismatch_count"] == 0
    assert report["audit_lifecycle_complete"] is True
    states = {(i["resource_type"], i["resource_id"]): i for i in report["items"]}
    assert states[("announcements", old)]["execution_state"] == "deleted"
    assert states[("announcements", old)]["record_still_exists"] is False
    assert states[("announcements", kept)]["execution_state"] == "retained"
    assert states[("announcements", kept)]["record_still_exists"] is True
    actions = [event["action"] for event in report["audit_trail"]]
    assert "retention.batch.candidates" in actions
    assert "retention.batch.confirm" in actions
    assert "retention.batch.execute" in actions
