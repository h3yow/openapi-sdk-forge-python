"""向后兼容性判定测试：破坏性变更为主，验证结论与字段级定位。"""

import copy
import json
import logging

import pytest

from sdk_forge.contract import parse_contract
from sdk_forge.errors import UnknownRulesVersionError
from sdk_forge.versioning import (
    RULES_VERSION,
    RULES_VERSION_2026_09,
    available_rules_versions,
    check_backward_compatible,
    check_with_rules,
    compare_across_rules,
)

logger = logging.getLogger("versioning-tests")


def make_spec(*, item_props, item_required=None, enum_values=None,
             path="/items/{id}", extra_responses=None, query_required=False):
    item_schema = {
        "type": "object",
        "required": item_required or ["id", "name"],
        "properties": item_props,
    }
    responses = {
        "200": {
            "description": "ok",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}},
        }
    }
    if extra_responses:
        responses.update(extra_responses)
    schemas = {"Item": item_schema}
    if enum_values is not None:
        schemas["Color"] = {"type": "string", "enum": enum_values}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Demo", "version": "1.0.0"},
        "paths": {
            path: {
                "get": {
                    "operationId": "get_item",
                    "tags": ["items"],
                    "parameters": [
                        {"name": "id", "in": "path", "required": True,
                         "schema": {"type": "integer"}},
                        {"name": "verbose", "in": "query",
                         "required": query_required, "schema": {"type": "boolean"}},
                    ],
                    "responses": responses,
                }
            }
        },
        "components": {"schemas": schemas},
    }


BASE_PROPS = {
    "id": {"type": "integer"},
    "name": {"type": "string"},
    "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
}


def _judge(old_spec, new_spec):
    logger.info("旧契约:\n%s", json.dumps(old_spec, ensure_ascii=False))
    logger.info("新契约:\n%s", json.dumps(new_spec, ensure_ascii=False))
    report = check_backward_compatible(
        parse_contract(old_spec), parse_contract(new_spec)
    )
    logger.info("判定依据(兼容=%s, rules=%s):\n接受: %s\n拒绝: %s",
                report.compatible, report.rules_version,
                [a.format() for a in report.accepted_changes],
                [c.format() for c in report.breaking_changes])
    return report


def test_add_optional_field_is_compatible():
    new = make_spec(item_props=dict(BASE_PROPS, extra={"type": "string"}))
    report = _judge(make_spec(item_props=dict(BASE_PROPS)), new)
    assert report.compatible is True
    assert any("新增响应字段" in a.detail for a in report.accepted_changes)


def test_remove_field_is_breaking():
    props = dict(BASE_PROPS)
    del props["name"]
    report = _judge(make_spec(item_props=dict(BASE_PROPS)),
                    make_spec(item_props=props))
    assert report.compatible is False
    kinds = {(c.kind, c.location) for c in report.breaking_changes}
    assert ("field_removed", "GET /items/{id} -> 响应 200.name") in kinds


def test_make_field_required_is_breaking():
    # note 旧可选 -> 新必填（响应方向：消费者旧客户端不依赖其存在，必填放宽；
    # 这里构造请求方向：POST body）
    def post_spec(required):
        return {
            "openapi": "3.1.0",
            "info": {"title": "Demo", "version": "1.0.0"},
            "paths": {"/items": {"post": {
                "operationId": "create", "tags": ["items"],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}},
                "responses": {"201": {"description": "created"}}}}},
            "components": {"schemas": {"Body": {
                "type": "object", "required": required,
                "properties": {"name": {"type": "string"},
                               "note": {"type": "string"}}}}},
        }
    report = _judge(post_spec(["name"]), post_spec(["name", "note"]))
    assert report.compatible is False
    notes = [c for c in report.breaking_changes
             if c.kind == "required_added" and "note" in c.location]
    assert len(notes) == 1  # 不得双重计数


def test_type_widening_int_to_number_accepted():
    new_props = dict(BASE_PROPS)
    new_props["id"] = {"type": "number"}
    report = _judge(make_spec(item_props=dict(BASE_PROPS)),
                    make_spec(item_props=new_props))
    assert report.compatible is True
    assert any("放宽" in a.detail for a in report.accepted_changes)


def test_type_narrowing_string_to_integer_rejected():
    new_props = dict(BASE_PROPS)
    new_props["name"] = {"type": "integer"}
    report = _judge(make_spec(item_props=dict(BASE_PROPS)),
                    make_spec(item_props=new_props))
    assert report.compatible is False
    change = next(c for c in report.breaking_changes if c.kind == "type_narrowed")
    assert "name" in change.location
    assert "string" in change.detail and "integer" in change.detail


def test_enum_narrowing_rejected_with_values():
    old = make_spec(item_props=dict(BASE_PROPS, color={"$ref": "#/components/schemas/Color"}),
                    enum_values=["red", "green", "blue"])
    new = make_spec(item_props=dict(BASE_PROPS, color={"$ref": "#/components/schemas/Color"}),
                    enum_values=["red", "green"])
    report = _judge(old, new)
    assert report.compatible is False
    change = next(c for c in report.breaking_changes if c.kind == "enum_narrowed")
    assert "blue" in change.detail


def test_enum_widening_accepted():
    old = make_spec(item_props=dict(BASE_PROPS, color={"$ref": "#/components/schemas/Color"}),
                    enum_values=["red"])
    new = make_spec(item_props=dict(BASE_PROPS, color={"$ref": "#/components/schemas/Color"}),
                    enum_values=["red", "blue"])
    report = _judge(old, new)
    assert report.compatible is True


def test_endpoint_removed_is_breaking():
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS))
    new["paths"]["/legacy"] = {"get": {
        "operationId": "legacy", "responses": {"200": {"description": "ok"}}}}
    report = _judge(new, old)  # old 有 /legacy，new 没有
    assert report.compatible is False
    assert report.breaking_changes[0].kind == "endpoint_removed"
    assert "/legacy" in report.breaking_changes[0].location


def test_required_query_param_added_rejected():
    report = _judge(
        make_spec(item_props=dict(BASE_PROPS), query_required=False),
        make_spec(item_props=dict(BASE_PROPS), query_required=True),
    )
    assert report.compatible is False
    assert any(c.kind == "required_added" and "verbose" in c.location
               for c in report.breaking_changes)


def test_response_status_removed_rejected():
    report = _judge(
        make_spec(item_props=dict(BASE_PROPS),
                  extra_responses={"404": {"description": "missing"}}),
        make_spec(item_props=dict(BASE_PROPS)),
    )
    assert report.compatible is False
    assert any(c.kind == "response_removed" and "404" in c.location
               for c in report.breaking_changes)


def test_nullable_to_nonnullable_narrowing():
    new_props = dict(BASE_PROPS)
    new_props["note"] = {"type": "string"}
    report = _judge(make_spec(item_props=dict(BASE_PROPS)),
                    make_spec(item_props=new_props))
    assert report.compatible is False
    assert any("可空" in c.detail for c in report.breaking_changes)


def test_rules_version_recorded_and_replayable(caplog):
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS, id2={"type": "string"}) )
    report = check_backward_compatible(parse_contract(old), parse_contract(new))
    assert report.rules_version == RULES_VERSION
    replayed = check_with_rules(
        parse_contract(old), parse_contract(new), RULES_VERSION
    )
    # 复现结论必须逐字段一致
    assert replayed.to_dict() | {"checked_at": report.checked_at} == (
        report.to_dict()
    )
    assert replayed.checked_at == ""  # 复现不盖时间戳
    # 不存在的规则版本：可区分错误，绝不静默按当前规则处理
    with pytest.raises(UnknownRulesVersionError) as exc:
        check_with_rules(parse_contract(old), parse_contract(new), "1999.01.01")
    assert exc.value.rules_version == "1999.01.01"
    assert RULES_VERSION in exc.value.available
    # 同时保持 ValueError 兼容
    with pytest.raises(ValueError, match="未知的规则版本"):
        check_with_rules(parse_contract(old), parse_contract(new), "1999.01.01")


def test_logs_contain_contract_input_and_basis(caplog):
    caplog.set_level(logging.INFO, logger="versioning-tests")
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props={k: v for k, v in BASE_PROPS.items() if k != "name"})
    _judge(old, new)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "旧契约" in text and "新契约" in text
    assert "判定依据" in text and "field_removed" in text


def test_malformed_contract_in_judgement_is_distinct():
    """畸形契约参与兼容性判定时，抛出的是 malformed_contract 而非普通断言。"""
    from sdk_forge.errors import MalformedContractError

    bad_spec = {"openapi": "3.1.0", "info": {"title": "x"}, "paths": {}}
    good = make_spec(item_props=dict(BASE_PROPS))
    with pytest.raises(MalformedContractError):
        check_backward_compatible(parse_contract(bad_spec), parse_contract(good))


def test_response_required_to_optional_is_breaking_under_current_rules():
    """响应方向：必填 -> 可选，新规则判破坏并定位接口/字段/方向。"""
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS), item_required=["id"])
    report = _judge(old, new)  # name 由必填变可选
    assert report.compatible is False
    change = next(
        c for c in report.breaking_changes
        if c.kind == "required_removed" and c.location.endswith(".name")
    )
    assert change.direction == "response"
    assert "GET /items/{id} -> 响应 200" in change.location
    assert "name" in change.detail


def test_response_optional_to_required_is_accepted():
    """响应方向：可选 -> 必填 是更强保证，接受。"""
    # note 旧可选 -> 新必填（响应）
    old = make_spec(item_props=dict(BASE_PROPS), item_required=["id", "name"])
    new = make_spec(
        item_props=dict(BASE_PROPS), item_required=["id", "name", "note"]
    )
    report = _judge(old, new)
    assert report.compatible is True
    assert any(
        a.kind == "required_strengthened"
        and a.direction == "response" and a.location.endswith(".note")
        for a in report.accepted_changes
    )


def test_request_required_to_optional_is_accepted():
    """同一处必填->可选变化在请求方向仍然是放宽、接受。"""
    def post_spec(required):
        return {
            "openapi": "3.1.0",
            "info": {"title": "Demo", "version": "1.0.0"},
            "paths": {"/items": {"post": {
                "operationId": "create", "tags": ["items"],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}},
                "responses": {"201": {"description": "created"}}}}},
            "components": {"schemas": {"Body": {
                "type": "object", "required": required,
                "properties": {"name": {"type": "string"},
                               "note": {"type": "string"}}}}},
        }
    report = _judge(post_spec(["name", "note"]), post_spec(["name"]))
    assert report.compatible is True
    assert any(
        a.kind == "required_relaxed"
        and a.direction == "request" and a.location.endswith(".note")
        for a in report.accepted_changes
    )


def test_same_required_flip_opposite_conclusion_by_direction():
    """同一处必填/可选变化：请求方向接受、响应方向拒绝。"""
    def body_spec(required, *, where):
        base = {
            "openapi": "3.1.0",
            "info": {"title": "Demo", "version": "1.0.0"},
            "paths": {},
            "components": {"schemas": {"Shape": {
                "type": "object", "required": required,
                "properties": {"name": {"type": "string"},
                               "note": {"type": "string"}}}}},
        }
        if where == "request":
            base["paths"]["/items"] = {"post": {
                "operationId": "create", "tags": ["items"],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Shape"}}}},
                "responses": {"201": {"description": "created"}}}}
        else:
            base["paths"]["/items/{id}"] = {"get": {
                "operationId": "get_item", "tags": ["items"],
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok", "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Shape"}}}}}}}
        return base

    req_report = check_backward_compatible(
        parse_contract(body_spec(["name", "note"], where="request")),
        parse_contract(body_spec(["name"], where="request")),
    )
    resp_report = check_backward_compatible(
        parse_contract(body_spec(["name", "note"], where="response")),
        parse_contract(body_spec(["name"], where="response")),
    )
    assert req_report.compatible is True
    assert resp_report.compatible is False
    rc = next(c for c in resp_report.breaking_changes if c.kind == "required_removed")
    assert rc.direction == "response" and rc.location.endswith(".note")
    assert all(c.direction != "request" for c in resp_report.breaking_changes)


def test_response_field_removal_and_type_narrowing_still_breaking():
    old = make_spec(item_props=dict(BASE_PROPS))
    # 删除 name + id 类型收窄
    new_props = dict(BASE_PROPS)
    del new_props["name"]
    new_props["id"] = {"type": "string"}
    report = _judge(old, make_spec(item_props=new_props))
    assert report.compatible is False
    kinds = {(c.kind, c.direction) for c in report.breaking_changes}
    assert ("field_removed", "response") in kinds
    assert ("type_narrowed", "response") in kinds


def test_array_element_type_narrowing_rejected():
    old_props = dict(BASE_PROPS)
    old_props["scores"] = {"type": "array", "items": {"type": "number"}}
    new_props = dict(BASE_PROPS)
    new_props["scores"] = {"type": "array", "items": {"type": "integer"}}
    report = _judge(make_spec(item_props=old_props),
                    make_spec(item_props=new_props))
    assert report.compatible is False
    assert any("scores[]" in c.location for c in report.breaking_changes)


def test_add_endpoint_is_compatible():
    new = make_spec(item_props=dict(BASE_PROPS))
    new["paths"]["/extra"] = {"get": {
        "operationId": "extra", "tags": ["items"],
        "responses": {"200": {"description": "ok"}}}}
    report = _judge(make_spec(item_props=dict(BASE_PROPS)), new)
    assert report.compatible is True
    assert any(a.detail == "新增接口" and a.direction == "endpoint"
               for a in report.accepted_changes)


# ---------------------------------------------------------------------------
# 规则集冻结 / 回溯 / 跨版本对比
# ---------------------------------------------------------------------------

def _required_flip_response_specs():
    """响应字段 name 必填 -> 可选 的同一对契约（两套规则结论应不同）。"""
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS), item_required=["id"])
    return old, new


def test_two_frozen_rules_versions_have_real_difference():
    assert RULES_VERSION_2026_09 == "2026.09.01"
    assert RULES_VERSION == "2026.10.01"
    assert available_rules_versions() == ["2026.09.01", "2026.10.01"]


def test_same_evolution_replays_differently_across_frozen_rules():
    old, new = _required_flip_response_specs()
    old_ir, new_ir = parse_contract(old), parse_contract(new)

    old_rules = check_with_rules(old_ir, new_ir, RULES_VERSION_2026_09)
    new_rules = check_with_rules(old_ir, new_ir, RULES_VERSION)

    # 旧规则：放宽接受；新规则：破坏
    assert old_rules.compatible is True
    assert new_rules.compatible is False
    assert any(
        a.kind == "required_relaxed" and a.direction == "response"
        for a in old_rules.accepted_changes
    )
    rc = next(c for c in new_rules.breaking_changes
              if c.kind == "required_removed")
    assert rc.direction == "response" and rc.location.endswith(".name")

    # 同版本重复复现：结论/定位/类别/依据逐字节一致，无时间戳干扰
    old_rules_again = check_with_rules(old_ir, new_ir, RULES_VERSION_2026_09)
    new_rules_again = check_with_rules(old_ir, new_ir, RULES_VERSION)
    assert old_rules.to_dict() == old_rules_again.to_dict()
    assert new_rules.to_dict() == new_rules_again.to_dict()
    assert old_rules.checked_at == "" and new_rules.checked_at == ""

    # 结论互不相同（证明两套规则确实冻结了不同行为）
    assert old_rules.to_dict() != new_rules.to_dict()


def test_compare_across_rules_shows_both_verdicts():
    old, new = _required_flip_response_specs()
    compared = compare_across_rules(parse_contract(old), parse_contract(new))
    logger.info("跨规则版本对比:\n%s", {
        v: {"compatible": r.compatible,
            "breaking": [c.format() for c in r.breaking_changes],
            "accepted": [a.format() for a in r.accepted_changes]}
        for v, r in compared.items()
    })
    assert list(compared) == ["2026.09.01", "2026.10.01"]
    assert compared["2026.09.01"].compatible is True
    assert compared["2026.10.01"].compatible is False

    # 未知版本：明确报错且不做部分返回
    with pytest.raises(UnknownRulesVersionError):
        compare_across_rules(
            parse_contract(old), parse_contract(new),
            ["2026.09.01", "3000.01.01"],
        )


def test_replay_independent_of_execution_order():
    old, new = _required_flip_response_specs()
    old_ir, new_ir = parse_contract(old), parse_contract(new)
    first = check_with_rules(old_ir, new_ir, RULES_VERSION)
    # 先跑一遍新规则、若干其它判定后再复现旧规则
    check_backward_compatible(old_ir, new_ir)
    compare_across_rules(old_ir, new_ir)
    again = check_with_rules(old_ir, new_ir, RULES_VERSION)
    assert first.to_dict() == again.to_dict()


# ---------------------------------------------------------------------------
# 回归：嵌套对象新增必填字段在冻结旧规则下的复现（bugfix-a）
# ---------------------------------------------------------------------------

def _nested_required_evolution():
    """请求体嵌套对象 Item 新增必填字段 extra2 的同一对契约。"""
    def spec(version, item_required, with_extra2=False):
        item_props = {"sku": {"type": "string"}, "extra": {"type": "string"}}
        if with_extra2:
            item_props["extra2"] = {"type": "string"}
        return {
            "openapi": "3.1.0",
            "info": {"title": "Demo", "version": version},
            "paths": {"/orders": {"post": {
                "operationId": "create_order", "tags": ["orders"],
                "requestBody": {"required": True, "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/Order"}}}},
                "responses": {"201": {"description": "created"}}}}},
            "components": {"schemas": {
                "Order": {"type": "object", "required": ["item"],
                          "properties": {
                              "item": {"$ref": "#/components/schemas/Item"},
                              "note": {"type": "string"}}},
                "Item": {"type": "object", "required": item_required,
                         "properties": item_props},
            }},
        }
    old = spec("1.0.0", ["sku"])
    new = spec("2.0.0", ["sku", "extra2"], with_extra2=True)
    return old, new


def test_nested_required_field_replay_matches_frozen_2026_09():
    """嵌套新增必填字段：旧规则版本归档结论是兼容，重跑必须原样重现。

    2026.09.01 发布时，请求方向「新增必填字段判破坏」只覆盖请求体顶层，
    嵌套对象的必填新增按放宽接受；该语义随规则版本冻结，重跑不得变成破坏。
    """
    old, new = _nested_required_evolution()
    old_ir, new_ir = parse_contract(old), parse_contract(new)
    logger.info("业务场景: 请求体嵌套对象新增必填字段; 用例: 旧规则复现 + 新旧规则对比")
    logger.info("旧契约:\n%s\n新契约:\n%s",
                json.dumps(old, ensure_ascii=False),
                json.dumps(new, ensure_ascii=False))

    frozen = check_with_rules(old_ir, new_ir, RULES_VERSION_2026_09)
    logger.info("2026.09.01 复现结论: compatible=%s accepted=%s breaking=%s",
                frozen.compatible,
                [a.format() for a in frozen.accepted_changes],
                [c.format() for c in frozen.breaking_changes])
    assert frozen.compatible is True, "旧规则归档结论是兼容，复现不得变成破坏"
    assert frozen.breaking_changes == []
    assert any(
        a.location.endswith("请求体.item.extra2") and a.direction == "request"
        for a in frozen.accepted_changes
    ), "嵌套必填新增在旧规则下应登记为接受项，定位到嵌套字段"

    # 同一规则版本任意时刻重跑：结论/定位/类别/依据逐字节一致
    frozen_again = check_with_rules(old_ir, new_ir, RULES_VERSION_2026_09)
    assert frozen.to_dict() == frozen_again.to_dict()
    assert frozen.checked_at == ""

    # 当前规则更严：同一演进判破坏，定位/方向/类别精确到嵌套字段
    current = check_with_rules(old_ir, new_ir, RULES_VERSION)
    logger.info("2026.10.01 结论: compatible=%s breaking=%s",
                current.compatible,
                [c.format() for c in current.breaking_changes])
    assert current.compatible is False
    change = next(c for c in current.breaking_changes
                  if c.kind == "required_added")
    assert change.direction == "request"
    assert change.location.endswith("请求体.item.extra2")

    # 跨规则版本对比一次跑出各版本各自结论
    compared = compare_across_rules(old_ir, new_ir)
    assert compared[RULES_VERSION_2026_09].to_dict() == frozen.to_dict()
    assert compared[RULES_VERSION].to_dict() == current.to_dict()


def test_top_level_required_field_still_breaking_under_all_rules():
    """对照用例：请求体顶层新增必填字段，新旧规则都必须判破坏（不得过度放宽）。"""
    def spec(version, top_required, with_coupon=False):
        props = {"item": {"type": "object",
                          "properties": {"sku": {"type": "string"}},
                          "required": ["sku"]},
                 "note": {"type": "string"}}
        if with_coupon:
            props["coupon"] = {"type": "string"}
        return {
            "openapi": "3.1.0", "info": {"title": "Demo", "version": version},
            "paths": {"/orders": {"post": {
                "operationId": "create_order", "tags": ["orders"],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": top_required,
                        "properties": props}}}},
                "responses": {"201": {"description": "created"}}}}},
        }
    old = spec("1.0.0", ["item"])
    new = spec("2.0.0", ["item", "coupon"], with_coupon=True)
    old_ir, new_ir = parse_contract(old), parse_contract(new)
    logger.info("业务场景: 请求体顶层新增必填字段; 用例: 两个规则版本都必须判破坏")
    for version in (RULES_VERSION_2026_09, RULES_VERSION):
        report = check_with_rules(old_ir, new_ir, version)
        logger.info("规则 %s 结论: compatible=%s breaking=%s", version,
                    report.compatible,
                    [c.format() for c in report.breaking_changes])
        assert report.compatible is False
        assert any(c.kind == "required_added"
                   and c.location.endswith("请求体.coupon")
                   for c in report.breaking_changes)
