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


def _ping_spec(version, required_msg):
    return {
        "openapi": "3.1.0", "info": {"title": "D", "version": version},
        "paths": {"/ping": {"get": {"operationId": "ping", "tags": ["x"],
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {
                    "type": "object",
                    "required": ["msg"] if required_msg else [],
                    "properties": {"msg": {"type": "string"}}}}}}}}}},
    }


def test_rules_versions_endpoint_lists_frozen_and_current(client):
    resp = client.get("/forge/rules-versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] == "2026.09.30"
    assert body["versions"] == ["2026.09.01", "2026.09.30"]


def test_compare_rules_endpoint_shows_direction_opposite_conclusions(client):
    old = _ping_spec("1", True)
    new = _ping_spec("2", False)  # 响应字段必填 -> 可选
    logger.info("compare 旧契约: %s\n新契约: %s", old, new)
    resp = client.post("/forge/compare-rules",
                       json={"old_contract": old, "new_contract": new})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    logger.info("compare 判定依据: %s", body)
    assert body["2026.09.01"]["compatible"] is True
    assert body["2026.09.30"]["compatible"] is False
    change = body["2026.09.30"]["breaking_changes"][0]
    assert change["kind"] == "field_became_optional"
    assert change["direction"] == "response"
    assert change["location"].endswith("响应 200.msg")

    # 不存在的规则版本：明确 404 + 可区分 code，不按当前规则静默处理
    bad = client.post("/forge/compare-rules", json={
        "old_contract": old, "new_contract": new,
        "rules_versions": ["1999.01.01"],
    })
    assert bad.status_code == 404
    detail = bad.json()["detail"]
    assert detail["code"] == "unknown_rules_version"
    assert "1999.01.01" in detail["message"]
    assert detail["available"] == ["2026.09.01", "2026.09.30"]


def test_checks_run_list_get_and_idempotent(client):
    old = _ping_spec("1", True)
    new = _ping_spec("2", True)
    payload = {"old_contract": old, "new_contract": new,
               "rules_version": "2026.09.30"}
    r1 = client.post("/forge/checks", json=payload)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/forge/checks", json=payload)
    assert r2.status_code == 200
    assert r1.json() == r2.json()  # 重复触发不重算、不覆盖

    listing = client.get("/forge/checks").json()["checks"]
    assert len(listing) == 1
    rec = listing[0]
    assert rec["old_version"] == "1" and rec["new_version"] == "2"
    assert rec["rules_version"] == "2026.09.30"

    got = client.get("/forge/checks/2/2026.09.30/1")
    assert got.status_code == 200
    assert got.json()["new_version"] == "2"
    missing = client.get("/forge/checks/9/2026.09.30/1")
    assert missing.status_code == 404
