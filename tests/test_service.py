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


# ---------------------------------------------------------------------------
# 判定归档：幂等 / 并发去重 / 查询 / 跨规则版本对比
# ---------------------------------------------------------------------------

from sdk_forge.errors import (  # noqa: E402
    JudgementInProgressError,
    UnknownRulesVersionError,
)
from sdk_forge.versioning import RULES_VERSION, RULES_VERSION_2026_09  # noqa: E402


def _field_spec(version, required):
    return {
        "openapi": "3.1.0",
        "info": {"title": "Demo", "version": version},
        "paths": {"/ping": {"get": {
            "operationId": "ping", "tags": ["meta"],
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {
                    "type": "object", "required": required,
                    "properties": {"msg": {"type": "string"},
                                   "trace": {"type": "string"}}}}}}}}}},
    }


def test_judge_idempotent_dedup_and_queryable(tmp_path):
    svc = ForgeService()
    old = _field_spec("1.0.0", ["msg", "trace"])
    new = _field_spec("2.0.0", ["msg"])  # 响应字段必填->可选

    computes = {"n": 0}
    orig = ForgeService._compute_judgement

    def counting(self2, *a, **k):
        computes["n"] += 1
        return orig(self2, *a, **k)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ForgeService, "_compute_judgement", counting)

    r1 = svc.judge(old_contract=old, new_contract=new, output_dir=tmp_path)
    r2 = svc.judge(old_contract=old, new_contract=new, output_dir=tmp_path)
    r3 = svc.judge(old_contract=old, new_contract=new, output_dir=tmp_path,
                   wait=True)
    assert computes["n"] == 1, "重复触发不得重复计算"
    assert r1.to_dict() == r2.to_dict() == r3.to_dict()
    assert r1.compatible is False  # 当前规则：必填->可选破坏
    assert r1.breaking_changes[0].direction == "response"

    # 按契约版本 + 规则版本查回
    got = svc.get_judgement(
        output_dir=tmp_path, old_version="1.0.0",
        new_version="2.0.0", rules_version=RULES_VERSION,
    )
    assert got.to_dict() == r1.to_dict()
    records = svc.list_judgements(tmp_path)
    assert len(records) == 1 and records[0]["old_version"] == "1.0.0"
    monkeypatch.undo()


def test_judge_concurrent_non_wait_rejected_wait_reuses(tmp_path, monkeypatch):
    svc = ForgeService()
    old = _field_spec("1.0.0", ["msg", "trace"])
    new = _field_spec("2.0.0", ["msg"])

    entered = threading.Event()
    release = threading.Event()
    computes = {"n": 0}
    orig = ForgeService._compute_judgement

    def slow(self2, *a, **k):
        computes["n"] += 1
        entered.set()
        release.wait(timeout=5)
        return orig(self2, *a, **k)

    monkeypatch.setattr(ForgeService, "_compute_judgement", slow)

    outcomes: list = []

    def worker(fn):
        try:
            outcomes.append(("ok", fn()))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("err", exc))

    t1 = threading.Thread(target=worker, args=(
        lambda: svc.judge(old_contract=old, new_contract=new, output_dir=tmp_path),
    ))
    t1.start()
    assert entered.wait(2)

    # 并发且不等待：明确告知进行中，不能写半成品/串结果
    t2 = threading.Thread(target=worker, args=(
        lambda: svc.judge(old_contract=old, new_contract=new, output_dir=tmp_path),
    ))
    t2.start()
    t2.join(5)
    assert len(outcomes) == 1
    assert outcomes[0][0] == "err"
    assert isinstance(outcomes[0][1], JudgementInProgressError)
    assert outcomes[0][1].judgement_key

    # 等待方：worker 完成后复用同一结论
    t3 = threading.Thread(target=worker, args=(
        lambda: svc.judge(old_contract=old, new_contract=new,
                          output_dir=tmp_path, wait=True),
    ))
    t3.start()
    # 给 t3 一点时间进入等待
    time.sleep(0.05)
    release.set()
    t1.join(5)
    t3.join(5)

    final = [o for o in outcomes if o[0] == "ok"]
    assert len(final) == 2
    assert final[0][1].to_dict() == final[1][1].to_dict()
    assert computes["n"] == 1, "并发等待只允许计算一次"

    # 落盘是完整成品，无半成品目录
    root = tmp_path / "_judgements"
    leftovers = [p.name for p in root.iterdir() if p.name.startswith(".")]
    assert leftovers == []
    dirs = [p for p in root.iterdir() if p.is_dir()]
    assert len(dirs) == 1
    assert (dirs[0] / "report.json").exists()
    assert (dirs[0] / "old_contract.json").exists()
    assert (dirs[0] / "new_contract.json").exists()


def test_judge_concurrent_wait_propagates_failure(tmp_path, monkeypatch):
    svc = ForgeService()
    old = _field_spec("1.0.0", ["msg"])
    new = _field_spec("2.0.0", ["msg"])

    entered = threading.Event()
    release = threading.Event()

    def slow_compute(self2, *a, **k):
        entered.set()
        release.wait(timeout=5)
        # 模拟 worker 计算期失败（parse 已通过）
        raise RuntimeError("boom")

    monkeypatch.setattr(ForgeService, "_compute_judgement", slow_compute)
    result: list = []

    def waiter():
        try:
            svc.judge(old_contract=old, new_contract=new,
                      output_dir=tmp_path, wait=True)
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    tw = threading.Thread(target=waiter)
    tw.start()
    assert entered.wait(2)
    errors: list = []
    tw2 = threading.Thread(
        target=lambda: errors.append(
            _run_worker(svc, old, new, tmp_path, wait=False))
    )
    tw2.start()
    time.sleep(0.05)
    release.set()
    tw.join(5)
    tw2.join(5)
    assert isinstance(result[0], RuntimeError), "等待方应同步收到 worker 的失败"
    # 非等待并发方拿到明确的"进行中"，而不是半成品/串结果
    assert isinstance(errors[0], JudgementInProgressError)
    # 失败不留半成品
    root = tmp_path / "_judgements"
    assert not root.exists() or not any(root.rglob("report.json"))


def _run_worker(svc, old, new, tmp_path, wait=False):
    try:
        return svc.judge(old_contract=old, new_contract=new,
                         output_dir=tmp_path, wait=wait)
    except Exception as exc:  # noqa: BLE001
        return exc


def test_judge_unknown_rules_version_distinct(tmp_path):
    svc = ForgeService()
    with pytest.raises(UnknownRulesVersionError) as exc:
        svc.judge(old_contract=None, new_contract=SPEC_V1,
                  output_dir=tmp_path, rules_version="1999.01.01")
    assert exc.value.rules_version == "1999.01.01"
    assert RULES_VERSION in exc.value.available
    assert not (tmp_path / "_judgements").exists(), "非法版本不得落任何产物"


def test_judge_baseline_and_compare_rules(tmp_path):
    svc = ForgeService()
    base = svc.judge(old_contract=None, new_contract=SPEC_V1, output_dir=tmp_path)
    assert base.compatible is True
    assert base.rules_version == RULES_VERSION
    # 基线重复触发复用
    again = svc.judge(old_contract=None, new_contract=SPEC_V1, output_dir=tmp_path)
    assert base.to_dict() == again.to_dict()

    old = _field_spec("1.0.0", ["msg", "trace"])
    new = _field_spec("2.0.0", ["msg"])
    compared = svc.compare_rules(old_contract=old, new_contract=new)
    logger.info("跨规则对比(服务层): %s",
                {v: {"compatible": r.compatible,
                     "breaking": [c.format() for c in r.breaking_changes]}
                 for v, r in compared.items()})
    assert list(compared) == [RULES_VERSION_2026_09, RULES_VERSION]
    assert compared[RULES_VERSION_2026_09].compatible is True
    assert compared[RULES_VERSION].compatible is False

    # 分别归档后可列举出两条规则版本的记录（另有一条基线记录）
    svc.judge(old_contract=old, new_contract=new, output_dir=tmp_path,
              rules_version=RULES_VERSION_2026_09)
    svc.judge(old_contract=old, new_contract=new, output_dir=tmp_path,
              rules_version=RULES_VERSION)
    records = [r for r in svc.list_judgements(tmp_path)
               if r["old_version"] is not None]
    by_rules = {r["rules_version"]: r["compatible"] for r in records}
    assert by_rules == {RULES_VERSION_2026_09: True, RULES_VERSION: False}
