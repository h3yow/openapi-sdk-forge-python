"""编排层稳定性测试：不可达、版本不匹配、并发去重、原子性、复现。"""

import json
import logging
import socket
import threading
import time

import pytest

from sdk_forge import service as service_mod
from sdk_forge.errors import (
    GenerationInProgressError,
    IncompatibleContractError,
    ServiceUnreachableError,
    VersionMismatchError,
)
from sdk_forge.service import ForgeService

logger = logging.getLogger("service-tests")

SPEC_V1 = {
    "openapi": "3.1.0",
    "info": {"title": "Demo", "version": "1.0.0"},
    "paths": {"/ping": {"get": {
        "operationId": "ping", "tags": ["meta"],
        "responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"type": "object", "properties": {
                "msg": {"type": "string"}}, "required": ["msg"]}}}}}}}},
}

SPEC_V2_COMPAT = {
    "openapi": "3.1.0",
    "info": {"title": "Demo", "version": "2.0.0"},
    "paths": {"/ping": {"get": {
        "operationId": "ping", "tags": ["meta"],
        "responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"type": "object", "properties": {
                "msg": {"type": "string"}, "trace": {"type": "string"}},
                "required": ["msg"]}}}}}}}},
}

SPEC_V2_BREAKING = {
    "openapi": "3.1.0",
    "info": {"title": "Demo", "version": "2.0.0"},
    "paths": {"/ping": {"get": {
        "operationId": "ping", "tags": ["meta"],
        "responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"type": "object", "properties": {
                "code": {"type": "integer"}}, "required": ["code"]}}}}}}}},
}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_unreachable_service_leaves_no_output(tmp_path):
    svc = ForgeService()
    port = _free_port()
    with pytest.raises(ServiceUnreachableError):
        svc.generate(f"http://127.0.0.1:{port}", tmp_path, timeout=0.5)
    assert not any(tmp_path.iterdir()) or all(
        not p.name.startswith(".") for p in tmp_path.rglob("*")
    )


def test_version_mismatch_refused_and_no_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "fetch_openapi",
                        lambda url, timeout=5.0: SPEC_V1)
    svc = ForgeService()
    with pytest.raises(VersionMismatchError) as exc:
        svc.generate("http://x", tmp_path, expected_version="9.9.9")
    assert exc.value.expected == "9.9.9" and exc.value.actual == "1.0.0"
    assert list(tmp_path.iterdir()) == []


def test_generation_success_and_idempotent_repeat(tmp_path, monkeypatch):
    calls = {"n": 0}
    def fake_fetch(url, timeout=5.0):
        calls["n"] += 1
        return SPEC_V1
    monkeypatch.setattr(service_mod, "fetch_openapi", fake_fetch)
    svc = ForgeService()

    result = svc.generate("http://x", tmp_path)
    assert result["status"] == "generated"
    vdir = tmp_path / "1.0.0"
    assert (vdir / "manifest.json").exists()
    assert (vdir / "sdk.zip").exists()
    assert json.loads((vdir / "contract.json").read_text())["info"]["version"] == "1.0.0"

    # 破坏网络后再次请求：应直接复用已完成结果
    def boom(url, timeout=5.0):
        raise AssertionError("不应再次访问网络")
    monkeypatch.setattr(service_mod, "fetch_openapi", boom)
    result2 = svc.generate("http://x", tmp_path, expected_version="1.0.0")
    assert result2["status"] == "already_exists"


def test_concurrent_duplicate_trigger_is_rejected(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch(url, timeout=5.0):
        # 能执行到这里，说明 worker 已完成占槽；让一个调度片确保主线程可见
        time.sleep(0.05)
        entered.set()
        release.wait(timeout=5)
        return SPEC_V1
    monkeypatch.setattr(service_mod, "fetch_openapi", slow_fetch)

    svc = ForgeService()
    errors: list[Exception] = []

    def worker():
        try:
            svc.generate("http://x", tmp_path)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    assert entered.wait(2)
    # 编排层在进入流程时即登记占槽（先于 fetch 返回）
    deadline = time.time() + 2
    while not svc._inflight and time.time() < deadline:
        time.sleep(0.01)
    assert svc._inflight, "编排层未登记进行中的生成任务"
    # 第二个并发请求必须被明确拒绝（拉取阶段同样去重）
    with pytest.raises(GenerationInProgressError):
        svc.generate("http://x", tmp_path)
    release.set()
    t.join()
    assert errors == []
    assert (tmp_path / "1.0.0" / "manifest.json").exists()
    # 不存在半成品临时目录
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_failed_generation_cleans_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "fetch_openapi",
                        lambda url, timeout=5.0: SPEC_V1)
    svc = ForgeService()

    def boom(ir, package_name="generated_sdk"):
        raise RuntimeError("生成器内部失败")
    monkeypatch.setattr(service_mod, "generate_sdk", boom)
    with pytest.raises(RuntimeError):
        svc.generate("http://x", tmp_path)
    assert [p.name for p in tmp_path.iterdir() if not p.name.startswith(".")] == []
    assert [p.name for p in tmp_path.iterdir()] == []


def test_compatible_evolution_accepted_and_archived(tmp_path, monkeypatch):
    specs = iter([SPEC_V1, SPEC_V2_COMPAT])
    monkeypatch.setattr(service_mod, "fetch_openapi",
                        lambda url, timeout=5.0: next(specs))
    svc = ForgeService()
    svc.generate("http://x", tmp_path)
    report = svc.check_compatibility("http://x", tmp_path)
    logger.info("兼容判定依据: compatible=%s accepted=%s",
                report.compatible, report.accepted_changes)
    assert report.compatible is True
    assert (tmp_path / "2.0.0" / "checks").exists()


def test_breaking_evolution_raises_with_locations(tmp_path, monkeypatch):
    specs = iter([SPEC_V1, SPEC_V2_BREAKING])
    monkeypatch.setattr(service_mod, "fetch_openapi",
                        lambda url, timeout=5.0: next(specs))
    svc = ForgeService()
    svc.generate("http://x", tmp_path)
    with pytest.raises(IncompatibleContractError) as exc:
        svc.check_compatibility("http://x", tmp_path, raise_on_breaking=True)
    detail = str(exc.value)
    logger.info("破坏性判定依据:\n%s", detail)
    assert "field_removed" in detail and "msg" in detail
    assert "GET /ping" in detail


def test_replay_historical_judgement(tmp_path):
    svc = ForgeService()
    r1 = svc.replay(old_contract=SPEC_V1, new_contract=SPEC_V2_BREAKING)
    r2 = svc.replay(old_contract=SPEC_V1, new_contract=SPEC_V2_BREAKING)
    assert r1.compatible is False and r2.compatible is False
    # 复现必须逐字段一致（不盖时间戳）
    assert r1.to_dict() == r2.to_dict()
