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
