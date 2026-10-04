"""Web API 集成测试：自举生成、错误分类、兼容性端点与 zip 下载。"""

import io
import json
import logging
import socket
import threading
import time
import zipfile

import pytest
import uvicorn
from fastapi.testclient import TestClient

import main as main_mod

logger = logging.getLogger("api-tests")


@pytest.fixture
def client(tmp_path):
    app = main_mod.create_app(artifact_root=tmp_path / "artifacts")
    with TestClient(app) as c:
        yield c


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_contract_is_exposed(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["version"] == "1.0.0"
    assert "/items/{item_id}" in spec["paths"]


def test_generate_self_sdk_via_http(client, tmp_path):
    # 在真实端口启动同一 app，管理 API 再通过 HTTP 拉取该实例的契约
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        main_mod.create_app(artifact_root=tmp_path / "artifacts"),
        host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    time.sleep(1.2)
    try:
        resp = client.post("/forge/generate",
                           json={"base_url": f"http://127.0.0.1:{port}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "generated"
        assert body["version"] == "1.0.0"

        zip_resp = client.get("/forge/artifacts/1.0.0/sdk.zip")
        assert zip_resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
            names = zf.namelist()
        assert any(n.endswith("client.py") for n in names)
        assert any(n.endswith("models.py") for n in names)

        # 重复触发：不覆盖、不重新生成
        resp2 = client.post("/forge/generate", json={"expected_version": "1.0.0"})
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "already_exists"
    finally:
        server.should_exit = True


def test_generate_unreachable_returns_distinct_code(client):
    port = _free_port()
    resp = client.post("/forge/generate",
                       json={"base_url": f"http://127.0.0.1:{port}", "timeout": 0.5})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "service_unreachable"


def test_version_mismatch_returns_409(client, tmp_path):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        main_mod.create_app(artifact_root=tmp_path / "artifacts"),
        host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    try:
        resp = client.post("/forge/generate", json={
            "base_url": f"http://127.0.0.1:{port}",
            "expected_version": "0.0.9",
        })
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "version_mismatch"
        assert detail["message"]
    finally:
        server.should_exit = True


def test_replay_endpoint_breaking_classification(client):
    old = {
        "openapi": "3.1.0", "info": {"title": "D", "version": "1"},
        "paths": {"/a": {"get": {"operationId": "a", "tags": ["x"], "responses": {
            "200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "object", "required": ["f"],
                           "properties": {"f": {"type": "string"}}}}}}}}}},
    }
    new = json.loads(json.dumps(old))
    new["info"]["version"] = "2"
    new["paths"]["/a"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]["properties"] = {
        "g": {"type": "string"}}
    new["paths"]["/a"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]["required"] = ["g"]
    logger.info("replay 旧契约: %s\n新契约: %s", old, new)
    resp = client.post("/forge/replay", json={
        "old_contract": old, "new_contract": new})
    assert resp.status_code == 200
    data = resp.json()
    logger.info("replay 判定依据: %s", data)
    assert data["compatible"] is False
    assert any(c["kind"] == "field_removed" and c["location"].endswith(".f")
               for c in data["breaking_changes"])


def test_replay_endpoint_rejects_malformed_contract(client):
    bad = {"openapi": "2.0", "info": {"title": "x"}, "paths": {}}
    good = {
        "openapi": "3.1.0", "info": {"title": "x", "version": "1"},
        "paths": {"/a": {"get": {"operationId": "a", "tags": ["x"],
                                 "responses": {"200": {"description": "ok"}}}}},
    }
    resp = client.post("/forge/replay", json={"old_contract": bad, "new_contract": good})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "malformed_contract"


def _resp_required_flip_contracts():
    old = {
        "openapi": "3.1.0", "info": {"title": "D", "version": "1"},
        "paths": {"/a": {"get": {"operationId": "a", "tags": ["x"], "responses": {
            "200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "object", "required": ["f"],
                           "properties": {"f": {"type": "string"}}}}}}}}}},
    }
    new = json.loads(json.dumps(old))
    new["info"]["version"] = "2"
    new["paths"]["/a"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]["required"] = []
    return old, new


def test_compare_rules_endpoint_shows_divergence(client):
    old, new = _resp_required_flip_contracts()
    logger.info("compare-rules 旧契约: %s\n新契约: %s", old, new)
    resp = client.post("/forge/compare-rules",
                       json={"old_contract": old, "new_contract": new})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    logger.info("compare-rules 判定依据: %s", data)
    assert data["2026.09.01"]["compatible"] is True
    assert data["2026.10.01"]["compatible"] is False
    change = data["2026.10.01"]["breaking_changes"][0]
    assert change["kind"] == "required_removed"
    assert change["direction"] == "response"
    assert change["location"].endswith(".f")


def test_replay_unknown_rules_version_returns_400(client):
    old, new = _resp_required_flip_contracts()
    resp = client.post("/forge/replay", json={
        "old_contract": old, "new_contract": new,
        "rules_version": "1999.01.01"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "unknown_rules_version"
    assert detail["message"]


def test_judge_archive_and_list_endpoint(client):
    old, new = _resp_required_flip_contracts()
    resp = client.post("/forge/judge", json={
        "old_contract": old, "new_contract": new,
        "rules_version": "2026.10.01"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["compatible"] is False

    # 重复触发：复用已完成结论，不重新计算（结论一致）
    resp2 = client.post("/forge/judge", json={
        "old_contract": old, "new_contract": new})
    assert resp2.status_code == 200
    assert resp2.json() == resp.json()

    listed = client.get("/forge/judgements").json()["judgements"]
    assert any(
        j["old_version"] == "1" and j["new_version"] == "2"
        and j["rules_version"] == "2026.10.01" and j["compatible"] is False
        for j in listed
    )


# ---------------------------------------------------------------------------
# 回归：对外 HTTP 链路的并发触发与未知规则版本（bugfix-a）
# ---------------------------------------------------------------------------

def test_check_compatibility_unknown_rules_version_returns_400(client, tmp_path):
    logger.info("业务场景: HTTP 兼容性检查传入未知规则版本; "
                "用例: 400 + 可区分 code + 无残留")
    resp = client.post("/forge/check-compatibility",
                       json={"rules_version": "1999.01.01", "timeout": 0.5})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "unknown_rules_version"
    assert "1999.01.01" in detail["message"]
    # 不留任何残留结果
    root = tmp_path / "artifacts"
    assert not root.exists() or list(root.iterdir()) == []


def test_judge_endpoint_concurrent_trigger_only_progress_or_reuse(
    client, tmp_path, monkeypatch
):
    """并发触发同一组合：每个请求只会得到 200（同一份结论）或
    409 judgement_in_progress；不出现 5xx，不落半成品，不互相串结果。"""
    from sdk_forge.service import ForgeService

    old, new = _resp_required_flip_contracts()

    entered = threading.Event()
    release = threading.Event()
    orig = ForgeService._compute_judgement

    def slow_compute(self2, *a, **k):
        entered.set()
        release.wait(timeout=10)
        return orig(self2, *a, **k)

    monkeypatch.setattr(ForgeService, "_compute_judgement", slow_compute)
    logger.info("业务场景: HTTP 判定链路并发触发; "
                "用例: 8 并发 wait=false + 8 并发 wait=true")

    results: list = []

    def fire(wait_flag):
        resp = client.post("/forge/judge", json={
            "old_contract": old, "new_contract": new, "wait": wait_flag})
        results.append((resp.status_code, resp.json()))

    # 第一波波：wait=false，只许 200 或 409
    t0 = threading.Thread(target=fire, args=(False,))
    t0.start()
    assert entered.wait(5), "首个请求应成为 worker 并进入计算"
    barrier = threading.Barrier(7)
    def fire_sync():
        barrier.wait(timeout=5)
        fire(False)
    threads = [threading.Thread(target=fire_sync) for _ in range(7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    release.set()
    t0.join(10)

    codes = sorted(code for code, _ in results)
    logger.info("wait=false 并发响应码: %s", codes)
    assert all(c in (200, 409) for c in codes), f"不得出现内部错误: {codes}"
    assert 409 in codes, "并发下应至少有一个明确收到“正在进行中”"
    for code, body in results:
        if code == 409:
            assert body["detail"]["code"] == "judgement_in_progress"
    oks = [body for code, body in results if code == 200]
    assert oks and all(b == oks[0] for b in oks), "成功的并发响应必须是同一份结论"

    # 第二波：wait=true，全部复用同一份已完成结论
    results.clear()
    threads = [threading.Thread(target=fire, args=(True,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert all(code == 200 for code, _ in results)
    assert all(body == oks[0] for _, body in results), "复用结论必须与首次一致"

    # 归档只有一条记录，且磁盘无半成品
    listed = client.get("/forge/judgements").json()["judgements"]
    assert len(listed) == 1
    root = tmp_path / "artifacts"
    leftovers = [str(p) for p in root.rglob("*")
                 if p.name.startswith(".") or p.name.endswith(".old")]
    assert leftovers == []
    logger.info("并发后归档记录: %s", listed)
