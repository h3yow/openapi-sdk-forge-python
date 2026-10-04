"""并发触发兼容性检查的稳定性测试（对外链路 + 底层原子写）。

覆盖业务：兼容性检查链路在并发触发下的行为 ——
结果只会是「进行中」（409 judgement_in_progress）或复用同一份已完成结论，
不得出现内部错误（5xx）、半成品目录或互相串结果。
"""

import json
import logging
import threading
import time

import pytest

from main import create_app
from sdk_forge import service as service_mod
from sdk_forge.errors import JudgementInProgressError
from sdk_forge.service import ForgeService, _atomic_write_tree
from sdk_forge.versioning import RULES_VERSION

logger = logging.getLogger("concurrency-tests")

CONCURRENCY = 10

SPEC_V1 = {
    "openapi": "3.1.0",
    "info": {"title": "Shop", "version": "1.0.0"},
    "paths": {"/ping": {"get": {
        "operationId": "ping", "tags": ["meta"],
        "responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"type": "object", "properties": {
                "msg": {"type": "string"}}, "required": ["msg"]}}}}}}}},
}

SPEC_V2 = {
    "openapi": "3.1.0",
    "info": {"title": "Shop", "version": "2.0.0"},
    "paths": {"/ping": {"get": {
        "operationId": "ping", "tags": ["meta"],
        "responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"type": "object", "properties": {
                "msg": {"type": "string"}, "trace": {"type": "string"}},
                "required": ["msg"]}}}}}}}},
}


def _run_parallel(fn, n=CONCURRENCY):
    """并发执行 fn(i)，返回 [(outcome, value)]，outcome ∈ {ok, err}。"""
    results: list = []
    lock = threading.Lock()

    def worker(i):
        try:
            value = fn(i)
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(("err", exc))
        else:
            with lock:
                results.append(("ok", value))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(not t.is_alive() for t in threads), "并发线程未在限期内结束"
    return results


def _assert_no_partial_dirs(root):
    leftovers = [
        str(p) for p in root.rglob("*")
        if p.name.startswith(".") or p.name.endswith(".old")
    ]
    assert leftovers == [], f"存在半成品/备份残留: {leftovers}"


# ---------------------------------------------------------------------------
# 底层：同一目标目录的并发原子写
# ---------------------------------------------------------------------------

def test_atomic_write_tree_concurrent_same_target(tmp_path):
    """多线程对同一目录并发原子写：不得抛内部异常、不得留半成品。"""
    payload = json.dumps({"compatible": True}, sort_keys=True)
    for round_ in range(5):
        target = tmp_path / f"round{round_}" / "checks"
        _atomic_write_tree(target, {"compatibility.json": payload})
        results = _run_parallel(
            lambda i: _atomic_write_tree(
                target, {"compatibility.json": payload}
            )
        )
        errors = [v for o, v in results if o == "err"]
        assert errors == [], f"并发原子写出现内部错误: {errors!r}"
        assert (target / "compatibility.json").read_text() == payload
    _assert_no_partial_dirs(tmp_path)
    logger.info("并发原子写 5 轮 x %d 线程：无内部错误、无半成品", CONCURRENCY)


# ---------------------------------------------------------------------------
# 服务层：并发触发同一对契约 + 同一规则版本的兼容性检查
# ---------------------------------------------------------------------------

def test_check_compatibility_concurrent_single_computation(tmp_path, monkeypatch):
    """10 路并发 check_compatibility：全部拿到同一份结论，只计算一次。"""
    specs = {"current": SPEC_V1}
    monkeypatch.setattr(
        service_mod, "fetch_openapi",
        lambda url, timeout=5.0: specs["current"],
    )
    svc = ForgeService()
    svc.generate("http://x", tmp_path)  # 归档 1.0.0 契约
    specs["current"] = SPEC_V2

    computes = {"n": 0}
    orig = ForgeService._compute_judgement

    def slow_compute(self2, *a, **k):
        computes["n"] += 1
        time.sleep(0.1)  # 拉宽并发窗口，确保线程确实重叠
        return orig(self2, *a, **k)

    monkeypatch.setattr(ForgeService, "_compute_judgement", slow_compute)

    results = _run_parallel(
        lambda i: svc.check_compatibility("http://x", tmp_path)
    )
    errors = [v for o, v in results if o == "err"]
    assert errors == [], f"并发检查出现内部错误: {errors!r}"
    reports = [v for o, v in results if o == "ok"]
    assert len(reports) == CONCURRENCY
    first = reports[0].to_dict()
    assert all(r.to_dict() == first for r in reports), "并发请求之间串了结果"
    assert computes["n"] == 1, "同一组合并发触发不得重复计算"
    assert first["compatible"] is True

    # 归档与镜像各一份，且是完整成品
    mirror = tmp_path / "2.0.0" / "checks" / RULES_VERSION / "compatibility.json"
    assert json.loads(mirror.read_text()) == first
    _assert_no_partial_dirs(tmp_path)
    logger.info("并发 check_compatibility: %d 路全部复用同一结论, 计算次数=%d",
                len(reports), computes["n"])


# ---------------------------------------------------------------------------
# API 层：对外 /forge/judge 链路并发触发
# ---------------------------------------------------------------------------

def test_judge_endpoint_concurrent_in_progress_or_reused(tmp_path, monkeypatch):
    """并发 POST /forge/judge：每个请求要么 200 复用结论，要么 409 进行中。"""
    from fastapi.testclient import TestClient

    orig = ForgeService._compute_judgement

    def slow_compute(self2, *a, **k):
        time.sleep(0.2)  # 保证并发窗口足够重叠
        return orig(self2, *a, **k)

    monkeypatch.setattr(ForgeService, "_compute_judgement", slow_compute)

    app = create_app(artifact_root=tmp_path)
    client = TestClient(app)
    body = {"old_contract": SPEC_V1, "new_contract": SPEC_V2,
            "rules_version": RULES_VERSION}

    def call(i):
        # 一半请求显式等待，一半不等待：两种都是合法行为
        resp = client.post("/forge/judge", json=body | {"wait": i % 2 == 0})
        return resp

    responses = [v for o, v in _run_parallel(call)]
    by_status: dict[int, list] = {}
    for resp in responses:
        by_status.setdefault(resp.status_code, []).append(resp)
    logger.info("并发 /forge/judge 状态分布: %s",
                {code: len(rs) for code, rs in by_status.items()})

    assert set(by_status) <= {200, 409}, (
        f"并发触发只允许 200(复用结论) 或 409(进行中)，实际: {sorted(by_status)}"
    )
    for resp in by_status.get(409, []):
        assert resp.json()["detail"]["code"] == "judgement_in_progress"
    ok_bodies = [r.json() for r in by_status.get(200, [])]
    assert ok_bodies, "至少应有一个请求拿到结论"
    assert all(b == ok_bodies[0] for b in ok_bodies), "并发请求之间串了结果"

    # 并发结束后：归档只有一份完整成品，重复触发直接复用（不重复计算、不覆盖）
    root = tmp_path / "_judgements"
    dirs = [p for p in root.iterdir() if p.is_dir()]
    assert len(dirs) == 1
    for name in ("meta.json", "report.json",
                 "old_contract.json", "new_contract.json"):
        assert (dirs[0] / name).exists(), f"归档缺少 {name}"
    _assert_no_partial_dirs(tmp_path)

    again = client.post("/forge/judge", json=body)
    assert again.status_code == 200
    assert again.json() == ok_bodies[0], "重复触发必须复用已归档结论"
    logger.info("并发结束后归档 1 份完整成品，重复触发原样复用")


def test_judge_service_concurrent_wait_all_reuse(tmp_path, monkeypatch):
    """服务层 wait=True 并发：全部等到同一份结论，无人失败、无人重复计算。"""
    svc = ForgeService()
    computes = {"n": 0}
    orig = ForgeService._compute_judgement

    def slow_compute(self2, *a, **k):
        computes["n"] += 1
        time.sleep(0.1)
        return orig(self2, *a, **k)

    monkeypatch.setattr(ForgeService, "_compute_judgement", slow_compute)

    results = _run_parallel(
        lambda i: svc.judge(old_contract=SPEC_V1, new_contract=SPEC_V2,
                            output_dir=tmp_path, wait=True)
    )
    errors = [v for o, v in results if o == "err"]
    assert errors == [], f"wait=True 并发不应失败: {errors!r}"
    reports = [v.to_dict() for o, v in results if o == "ok"]
    assert len(reports) == CONCURRENCY
    assert all(r == reports[0] for r in reports)
    assert computes["n"] == 1
    _assert_no_partial_dirs(tmp_path)
    logger.info("wait=True 并发 %d 路全部复用同一结论，计算次数=%d",
                len(reports), computes["n"])
