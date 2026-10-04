"""规则版本冻结与历史复现测试。

覆盖业务：同一份契约演进（请求体嵌套对象新增必填字段）在已发布的旧规则
版本下，无论何时重跑都必须原样重现当初归档的结论、定位、类别与依据；
新规则版本可以更严，但不得改变旧版本的历史语义；跨规则版本对比一次跑出
各版本各自结论；未知规则版本给出可区分错误且不留残留。
"""

import json
import logging

import pytest

from sdk_forge import service as service_mod
from sdk_forge.contract import parse_contract
from sdk_forge.errors import UnknownRulesVersionError
from sdk_forge.service import ForgeService
from sdk_forge.versioning import (
    RULES_VERSION,
    RULES_VERSION_2026_09,
    check_with_rules,
    compare_across_rules,
)

logger = logging.getLogger("rules-freeze-tests")


def order_spec(version, *, nested_props, nested_required):
    """POST /orders：请求体第一层 customer 为嵌套对象。"""
    return {
        "openapi": "3.1.0",
        "info": {"title": "Shop", "version": version},
        "paths": {"/orders": {"post": {
            "operationId": "create_order", "tags": ["orders"],
            "requestBody": {"required": True, "content": {
                "application/json": {"schema": {
                    "type": "object", "required": ["customer"],
                    "properties": {"customer": {
                        "type": "object",
                        "required": nested_required,
                        "properties": nested_props}}}}}},
            "responses": {"201": {"description": "created"}}}}},
    }


# 演进1：嵌套对象里已有可选字段 email 变为必填
OLD_NESTED_FLIP = order_spec(
    "1.0.0",
    nested_props={"name": {"type": "string"}, "email": {"type": "string"}},
    nested_required=["name"],
)
NEW_NESTED_FLIP = order_spec(
    "2.0.0",
    nested_props={"name": {"type": "string"}, "email": {"type": "string"}},
    nested_required=["name", "email"],
)

# 演进2：嵌套对象里新增一个必填字段 phone
OLD_NESTED_ADD = order_spec(
    "1.0.0",
    nested_props={"name": {"type": "string"}},
    nested_required=["name"],
)
NEW_NESTED_ADD = order_spec(
    "2.0.0",
    nested_props={"name": {"type": "string"}, "phone": {"type": "string"}},
    nested_required=["name", "phone"],
)

NESTED_LOC = "POST /orders -> 请求体.customer"


def _log_report(tag, report):
    logger.info("%s: rules=%s compatible=%s\n接受: %s\n拒绝: %s",
                tag, report.rules_version, report.compatible,
                [a.format() for a in report.accepted_changes],
                [c.format() for c in report.breaking_changes])


@pytest.mark.parametrize("old_spec,new_spec,field", [
    (OLD_NESTED_FLIP, NEW_NESTED_FLIP, "email"),
    (OLD_NESTED_ADD, NEW_NESTED_ADD, "phone"),
], ids=["nested_optional_to_required", "nested_required_field_added"])
def test_nested_required_field_replays_archived_old_rules(old_spec, new_spec, field):
    """嵌套对象新增必填字段：旧规则版本原样重现归档结论（兼容）。"""
    old_ir, new_ir = parse_contract(old_spec), parse_contract(new_spec)

    archived = check_with_rules(old_ir, new_ir, RULES_VERSION_2026_09)
    _log_report("旧规则归档结论", archived)
    assert archived.compatible is True
    assert archived.breaking_changes == []
    assert archived.rules_version == RULES_VERSION_2026_09

    # 无论什么时候重跑：结论、定位、类别、依据逐字段一致
    for _ in range(3):
        rerun = check_with_rules(old_ir, new_ir, RULES_VERSION_2026_09)
        assert rerun.to_dict() == archived.to_dict()


@pytest.mark.parametrize("old_spec,new_spec,field", [
    (OLD_NESTED_FLIP, NEW_NESTED_FLIP, "email"),
    (OLD_NESTED_ADD, NEW_NESTED_ADD, "phone"),
], ids=["nested_optional_to_required", "nested_required_field_added"])
def test_nested_required_field_breaking_under_current_rules(
    old_spec, new_spec, field
):
    """同一演进在当前规则下判破坏，且定位/类别/方向准确。"""
    report = check_with_rules(
        parse_contract(old_spec), parse_contract(new_spec), RULES_VERSION
    )
    _log_report("当前规则结论", report)
    assert report.compatible is False
    change = next(c for c in report.breaking_changes
                  if c.kind == "required_added")
    assert change.direction == "request"
    assert change.location == f"{NESTED_LOC}.{field}"
    assert field in change.detail


def test_compare_across_rules_one_call_per_version_verdicts():
    """跨规则版本对比：一次调用拿到各版本各自结论（升级前影响面评估）。"""
    compared = compare_across_rules(
        parse_contract(OLD_NESTED_ADD), parse_contract(NEW_NESTED_ADD)
    )
    logger.info("跨规则对比: %s",
                {v: r.compatible for v, r in compared.items()})
    assert list(compared) == [RULES_VERSION_2026_09, RULES_VERSION]
    assert compared[RULES_VERSION_2026_09].compatible is True
    assert compared[RULES_VERSION].compatible is False
    # 各版本报告自带规则版本号，可独立归档
    assert compared[RULES_VERSION_2026_09].rules_version == RULES_VERSION_2026_09
    assert compared[RULES_VERSION].rules_version == RULES_VERSION


def test_top_level_request_required_still_breaking_under_old_rules():
    """旧规则并非整体放宽：请求体第一层新增必填字段两个版本都判破坏。"""
    old = order_spec("1.0.0", nested_props={"name": {"type": "string"}},
                     nested_required=["name"])
    new = order_spec("2.0.0", nested_props={"name": {"type": "string"}},
                     nested_required=["name"])
    for spec in (old, new):
        spec["paths"]["/orders"]["post"]["requestBody"]["content"][
            "application/json"]["schema"]["properties"]["note"] = {
            "type": "string"}
    new["paths"]["/orders"]["post"]["requestBody"]["content"][
        "application/json"]["schema"]["required"] = ["customer", "note"]
    for version in (RULES_VERSION_2026_09, RULES_VERSION):
        report = check_with_rules(
            parse_contract(old), parse_contract(new), version
        )
        _log_report(f"第一层必填演进 rules={version}", report)
        assert report.compatible is False
        assert any(c.kind == "required_added"
                   and c.location.endswith("请求体.note")
                   for c in report.breaking_changes)


def test_response_nested_checks_unchanged_under_old_rules():
    """响应方向的嵌套校验在旧规则下不受影响（冻结只涉及请求方向）。"""
    def resp_spec(version, nested_props):
        return {
            "openapi": "3.1.0",
            "info": {"title": "Shop", "version": version},
            "paths": {"/orders/{id}": {"get": {
                "operationId": "get_order", "tags": ["orders"],
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok", "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["customer"],
                        "properties": {"customer": {
                            "type": "object", "required": ["name"],
                            "properties": nested_props}}}}}}}}}},
        }
    old = resp_spec("1.0.0", {"name": {"type": "string"},
                              "email": {"type": "string"}})
    new = resp_spec("2.0.0", {"name": {"type": "string"}})  # 嵌套删字段
    for version in (RULES_VERSION_2026_09, RULES_VERSION):
        report = check_with_rules(
            parse_contract(old), parse_contract(new), version
        )
        assert report.compatible is False
        assert any(c.kind == "field_removed" and "email" in c.location
                   and c.direction == "response"
                   for c in report.breaking_changes)


# ---------------------------------------------------------------------------
# 服务层：归档 -> 重跑复现 -> 跨版本对比
# ---------------------------------------------------------------------------

def test_archived_old_rules_judgement_replayed_exactly(tmp_path):
    svc = ForgeService()
    archived = svc.judge(old_contract=OLD_NESTED_ADD, new_contract=NEW_NESTED_ADD,
                         output_dir=tmp_path,
                         rules_version=RULES_VERSION_2026_09)
    assert archived.compatible is True

    # 重新触发同一组合：直接复用归档，结论逐字段一致
    again = svc.judge(old_contract=OLD_NESTED_ADD, new_contract=NEW_NESTED_ADD,
                      output_dir=tmp_path,
                      rules_version=RULES_VERSION_2026_09)
    assert again.to_dict() == archived.to_dict()

    # 按版本查询与纯计算复现，同样逐字段一致
    fetched = svc.get_judgement(
        output_dir=tmp_path, old_version="1.0.0", new_version="2.0.0",
        rules_version=RULES_VERSION_2026_09,
    )
    assert fetched.to_dict() == archived.to_dict()
    replayed = svc.replay(old_contract=OLD_NESTED_ADD,
                          new_contract=NEW_NESTED_ADD,
                          rules_version=RULES_VERSION_2026_09)
    assert replayed.to_dict() == archived.to_dict()

    # 同一演进跨规则版本对比：旧版兼容、当前版本破坏
    compared = svc.compare_rules(old_contract=OLD_NESTED_ADD,
                                 new_contract=NEW_NESTED_ADD)
    assert compared[RULES_VERSION_2026_09].to_dict() == archived.to_dict()
    assert compared[RULES_VERSION].compatible is False
    logger.info("归档复现一致；跨规则对比: %s",
                {v: r.compatible for v, r in compared.items()})


def test_unknown_rules_version_leaves_no_residue(tmp_path, monkeypatch):
    """不存在的规则版本：可区分错误，且不留下任何残留结果。"""
    monkeypatch.setattr(service_mod, "fetch_openapi",
                        lambda url, timeout=5.0: SPEC_PING)
    svc = ForgeService()
    with pytest.raises(UnknownRulesVersionError) as exc:
        svc.check_compatibility("http://x", tmp_path,
                                rules_version="1999.01.01")
    assert exc.value.rules_version == "1999.01.01"
    assert RULES_VERSION_2026_09 in exc.value.available
    assert RULES_VERSION in exc.value.available
    # 无归档、无镜像、无任何目录残留
    assert not (tmp_path / "_judgements").exists()
    assert list(tmp_path.iterdir()) == []

    with pytest.raises(UnknownRulesVersionError):
        svc.compare_rules(old_contract=OLD_NESTED_ADD,
                          new_contract=NEW_NESTED_ADD,
                          versions=["1999.01.01"])
    logger.info("未知规则版本错误可区分且无残留: %s", exc.value)


SPEC_PING = {
    "openapi": "3.1.0",
    "info": {"title": "Shop", "version": "1.0.0"},
    "paths": {"/ping": {"get": {
        "operationId": "ping", "tags": ["meta"],
        "responses": {"200": {"description": "ok"}}}}},
}
