"""End-to-end forge API tests against a real HTTP server.

Covers: generation from a live contract, artifact retrieval, idempotent and
concurrent duplicate triggers, unreachable service, version mismatch,
compatibility verdicts with report history, and running the *generated* SDK
against the live demo service.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from sdk_forge.service import app

logger = logging.getLogger("test.service_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    store_dir = tmp_path_factory.mktemp("forge-store")
    import os
    os.environ["SDK_FORGE_STORE"] = str(store_dir)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/openapi.json", timeout=1) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("demo server failed to start")
    yield base_url, store_dir
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_demo_service_contract_is_complete(live_server):
    base_url, _ = live_server
    with urllib.request.urlopen(base_url + "/openapi.json") as resp:
        doc = json.loads(resp.read())
    assert doc["info"]["version"] == "1.0.0"
    ops = {
        (m, p): node["operationId"]
        for p, item in doc["paths"].items()
        for m, node in item.items()
        if m in ("get", "post", "put", "delete", "patch")
    }
    assert ("get", "/items/{item_id}") in ops
    assert ("post", "/items") in ops


def test_generate_from_live_service(client, live_server):
    base_url, _ = live_server
    resp = client.post("/forge/generate", json={"base_url": base_url})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "generated"
    assert body["version"] == "1.0.0"
    assert len(body["fingerprint"]) == 64
    assert "client.py" in body["files"]


def test_generated_artifact_is_downloadable(client, live_server):
    base_url, _ = live_server
    gen = client.post("/forge/generate", json={"base_url": base_url}).json()
    fp = gen["fingerprint"]
    resp = client.get(
        f"/forge/artifacts/default/{gen['version']}/{fp}/client.py"
    )
    assert resp.status_code == 200
    assert "class Client" in resp.text
    meta = client.get(
        f"/forge/artifacts/default/{gen['version']}/{fp}/codegen.json"
    ).json()
    assert meta["contract_fingerprint"] == fp


def test_generate_idempotent_second_call_returns_exists(client, live_server):
    base_url, _ = live_server
    first = client.post("/forge/generate", json={"base_url": base_url})
    assert first.status_code in (200, 201)
    second = client.post("/forge/generate", json={"base_url": base_url})
    assert second.status_code == 200
    assert second.json()["status"] == "exists"


def test_generate_unreachable_service_classified(client):
    resp = client.post("/forge/generate", json={"base_url": "http://127.0.0.1:1"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "service_unreachable"


def test_generate_version_mismatch_classified(client, live_server):
    base_url, _ = live_server
    resp = client.post(
        "/forge/generate", json={"base_url": base_url, "version": "9.9.9"}
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "version_mismatch"
    assert body["details"]["served_version"] == "1.0.0"


def test_generate_malformed_contract_classified(client, monkeypatch):
    from sdk_forge import service as svc
    broken = {"openapi": "3.1.0", "info": {"title": "x", "version": "1"}}
    monkeypatch.setattr(
        svc.contract_mod, "fetch_contract_raw",
        lambda url, timeout=10.0: (broken, url),
    )
    resp = client.post("/forge/generate", json={"base_url": "http://broken"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "malformed_contract"
    assert resp.json()["details"]["path"]


def test_concurrent_duplicate_generation_one_wins(client, live_server, monkeypatch):
    """Two identical parallel triggers: one result, one 409, no corruption."""
    import os
    import tempfile
    from sdk_forge import service as svc

    # Force a fresh target so the artifact does not already exist.
    target = f"race-{os.getpid()}"
    base_url, _ = live_server
    barrier = threading.Barrier(2, timeout=10)
    original_generate = svc.generate_sdk

    def slow_generate(contract):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.3)
        return original_generate(contract)

    monkeypatch.setattr(svc, "generate_sdk", slow_generate)

    results: dict = {}

    def fire(idx):
        r = client.post("/forge/generate", json={"base_url": base_url, "target": target})
        results[idx] = (r.status_code, r.json())

    t1 = threading.Thread(target=fire, args=(1,))
    t2 = threading.Thread(target=fire, args=(2,))
    t1.start()
    # stagger slightly so both arrive while the lock is held
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    statuses = sorted(results[i][0] for i in results)
    assert statuses == [201, 409], statuses
    assert any(body["error"] == "generation_in_progress"
               for _, body in results.values() if "error" in body)

    # the single completed artifact is intact and retrievable
    gen = client.post(
        "/forge/generate", json={"base_url": base_url, "target": target}
    ).json()
    fp = gen["fingerprint"]
    files = client.get(
        f"/forge/artifacts/{target}/{gen['version']}/{fp}/__init__.py"
    )
    assert files.status_code == 200


def test_compat_endpoint_accepts_safe_evolution_and_persists(client, live_server,
                                                             monkeypatch):
    from sdk_forge import service as svc
    base_url, _ = live_server
    with urllib.request.urlopen(base_url + "/openapi.json") as resp:
        raw = json.loads(resp.read())

    evolved = json.loads(json.dumps(raw))
    # add an optional, nullable field to the ItemModel (safe evolution)
    evolved["components"]["schemas"]["ItemModel"]["properties"]["description"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}], "title": "Description",
    }

    monkeypatch.setattr(
        svc.contract_mod, "fetch_contract_raw",
        lambda url, timeout=10.0: (evolved if "new" in url else raw, url),
    )
    resp = client.post("/forge/compat", json={
        "old_base_url": "http://old",
        "new_base_url": "http://new",
        "target": "demo-compat",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["compatible"] is True

    history = client.get("/forge/reports/demo-compat").json()["reports"]
    assert len(history) == 1
    assert history[0]["rule_version"] == body["rule_version"]


def test_compat_endpoint_rejects_field_removal(client, monkeypatch):
    from sdk_forge import service as svc

    def doc():
        return {
            "openapi": "3.1.0",
            "info": {"title": "Items Demo Service", "version": "1.0.0"},
            "paths": {
                "/items/{item_id}": {
                    "get": {
                        "operationId": "getItem",
                        "parameters": [
                            {"name": "item_id", "in": "path", "required": True,
                             "schema": {"type": "integer"}}
                        ],
                        "responses": {
                            "200": {"description": "ok", "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Item"}}}},
                        },
                    }
                }
            },
            "components": {"schemas": {"Item": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                "required": ["id", "name"],
            }}},
        }

    old_doc = doc()
    new_doc = doc()
    del new_doc["components"]["schemas"]["Item"]["properties"]["name"]
    new_doc["components"]["schemas"]["Item"]["required"] = ["id"]

    def fake_fetch(url, timeout=10.0):
        return (new_doc if "new" in url else old_doc, url)

    monkeypatch.setattr(svc.contract_mod, "fetch_contract_raw", fake_fetch)
    resp = client.post("/forge/compat", json={
        "old_base_url": "http://old", "new_base_url": "http://new",
        "target": "demo-breaking",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["compatible"] is False
    breaking = body["changes"]
    assert any(c["severity"] == "breaking" and "name" in c["location"]
               and c["rule_id"] == "R002-field-removed" for c in breaking)

    history = client.get("/forge/reports/demo-breaking").json()["reports"]
    assert history[0]["compatible"] is False


def test_generated_client_works_against_live_demo(client, live_server, tmp_path):
    """Smoke test: generated SDK can actually call the running service."""
    base_url, store_dir = live_server
    # dedicated target -> always a fresh 201 response carrying package metadata
    gen = client.post("/forge/generate",
                      json={"base_url": base_url, "target": "smoke"}).json()
    fp = gen["fingerprint"]
    version = gen["version"]
    pkg_dir = tmp_path / gen["package_name"]
    pkg_dir.mkdir()
    for rel in ("__init__.py", "models.py", "client.py", "codegen.json"):
        r = client.get(f"/forge/artifacts/smoke/{version}/{fp}/{rel}")
        (pkg_dir / rel).write_text(r.text)

    sys.path.insert(0, str(tmp_path))
    try:
        mod = importlib.import_module(gen["package_name"])
        importlib.reload(mod)
        sdk = mod.Client(base_url)

        # GET with path + query params; typed model response
        item = sdk.get_item(1)
        assert item.id == 1
        assert item.name == "hammer"
        verbose = sdk.get_item(2, verbose=True)
        assert verbose.tags == []

        # error status raises the generated ApiError
        with pytest.raises(mod.ApiError) as exc_info:
            sdk.get_item(999)
        assert exc_info.value.status_code == 404

        # POST with a typed request body model -> 201, typed model response
        created = sdk.create_item(body=mod.ItemCreate(name="drill", price=99.0))
        assert created.name == "drill"
        assert created.id >= 1
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(gen["package_name"], None)
