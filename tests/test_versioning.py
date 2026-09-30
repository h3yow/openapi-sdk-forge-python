"""向后兼容性判定测试：破坏性变更为主，验证结论与字段级定位。"""

import copy
import json
import logging

import pytest

from sdk_forge.contract import parse_contract
from sdk_forge.errors import UnknownRulesVersionError
from sdk_forge.versioning import (
    DIRECTION_REQUEST,
    DIRECTION_RESPONSE,
    RULES_VERSION,
    RULES_VERSION_V1,
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
                report.accepted_changes,
                [c.format() for c in report.breaking_changes])
    return report


def test_add_optional_field_is_compatible():
    new = make_spec(item_props=dict(BASE_PROPS, extra={"type": "string"}))
    report = _judge(make_spec(item_props=dict(BASE_PROPS)), new)
    assert report.compatible is True
    assert any("新增可选字段" in a for a in report.accepted_changes)


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
    assert any("放宽" in a for a in report.accepted_changes)


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


def test_unknown_rules_version_is_distinct_error():
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS, id2={"type": "string"}))
    # 必须是可区分的专用错误，且保持 ValueError 兼容；绝不静默按当前规则处理
    with pytest.raises(UnknownRulesVersionError) as exc:
        check_with_rules(parse_contract(old), parse_contract(new), "1999.01.01")
    assert isinstance(exc.value, ValueError)
    assert exc.value.rules_version == "1999.01.01"
    assert RULES_VERSION in exc.value.available
    assert RULES_VERSION_V1 in exc.value.available


def test_replay_is_identical_across_runs_and_order_independent():
    """复现不受执行顺序/时间戳影响：同输入多次、乱序跑均字节一致。"""
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS), item_required=["id"])
    r1 = check_with_rules(parse_contract(old), parse_contract(new), RULES_VERSION)
    # 先反向跑一次，再跑正向：结论不得被执行顺序污染
    check_with_rules(parse_contract(new), parse_contract(old), RULES_VERSION)
    r3 = check_with_rules(parse_contract(old), parse_contract(new), RULES_VERSION)
    assert r1.to_dict() == r3.to_dict()
    assert r1.compatible is False
    r4 = check_with_rules(parse_contract(old), parse_contract(new),
                          RULES_VERSION_V1)
    r5 = check_with_rules(parse_contract(old), parse_contract(new),
                          RULES_VERSION_V1)
    assert r4.to_dict() == r5.to_dict()


def test_compare_across_rules_shows_version_difference():
    """同一份演进在两套规则下结论不同，并可逐字段对比、稳定复现。"""
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS), item_required=["id"])
    logger.info("跨版本对比 旧契约:\n%s", json.dumps(old, ensure_ascii=False))
    logger.info("跨版本对比 新契约:\n%s", json.dumps(new, ensure_ascii=False))
    results = compare_across_rules(parse_contract(old), parse_contract(new))
    assert set(results) == {RULES_VERSION_V1, RULES_VERSION}
    by_version = {r.rules_version: r for r in results.values()}
    old_rules = by_version[RULES_VERSION_V1]
    new_rules = by_version[RULES_VERSION]
    logger.info("旧规则结论: %s", old_rules.to_dict())
    logger.info("新规则结论: %s", new_rules.to_dict())
    assert old_rules.compatible is True
    assert new_rules.compatible is False
    assert [c.kind for c in new_rules.breaking_changes] == [
        "field_became_optional"
    ]
    assert available_rules_versions() == [RULES_VERSION_V1, RULES_VERSION]
    # 对比接口对未知版本同样报可区分错误
    with pytest.raises(UnknownRulesVersionError):
        compare_across_rules(parse_contract(old), parse_contract(new),
                             ["nope"])


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
    """响应方向：必填 -> 可选是破坏（新规则），定位到接口+响应+字段。"""
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS), item_required=["id"])
    report = _judge(old, new)  # name 由必填变可选（响应字段）
    assert report.compatible is False
    hits = [c for c in report.breaking_changes
            if c.kind == "field_became_optional"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit.direction == DIRECTION_RESPONSE
    assert hit.location == "GET /items/{id} -> 响应 200.name"
    assert "必填变为可选" in hit.detail


def test_request_required_to_optional_is_accepted_under_current_rules():
    """请求方向：必填 -> 可选是放宽，同一类翻转与响应方向结论相反。"""
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
    assert not [c for c in report.breaking_changes
                if c.kind == "field_became_optional"]
    assert any("必填变为可选" in a for a in report.accepted_changes)


def test_response_optional_to_required_is_accepted_under_current_rules():
    """响应方向：可选 -> 必填是服务端更强保证，接受。"""
    old = make_spec(item_props=dict(BASE_PROPS), item_required=["id", "name"])
    new = make_spec(
        item_props=dict(BASE_PROPS, extra={"type": "string"}),
        item_required=["id", "name", "extra"],
    )
    report = _judge(old, new)
    assert report.compatible is True
    assert any("extra" in a and "必填" in a for a in report.accepted_changes)


def test_request_optional_to_required_is_breaking_under_current_rules():
    """请求方向：可选 -> 必填仍是破坏，direction 标 request。"""
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
                "properties": {"name": {"type": "string"}}}}},
        }
    report = _judge(post_spec([]), post_spec(["name"]))
    assert report.compatible is False
    hits = [c for c in report.breaking_changes if c.kind == "required_added"]
    assert hits and all(c.direction == DIRECTION_REQUEST for c in hits)


def test_response_field_removed_and_type_narrowing_still_breaking():
    """响应方向既有的拒绝项不放松：删除响应字段、响应类型收窄。"""
    removed = make_spec(item_props={k: v for k, v in BASE_PROPS.items() if k != "name"})
    report = _judge(make_spec(item_props=dict(BASE_PROPS)), removed)
    assert report.compatible is False
    hit = next(c for c in report.breaking_changes if c.kind == "field_removed")
    assert hit.direction == DIRECTION_RESPONSE
    assert "GET /items/{id}" in hit.location

    narrowed_props = dict(BASE_PROPS)
    narrowed_props["name"] = {"type": "integer"}
    report2 = _judge(make_spec(item_props=dict(BASE_PROPS)),
                     make_spec(item_props=narrowed_props))
    assert report2.compatible is False
    hit2 = next(c for c in report2.breaking_changes if c.kind == "type_narrowed")
    assert hit2.direction == DIRECTION_RESPONSE


def test_nested_new_required_request_field_is_breaking_under_current_rules():
    """新规则：请求体任意深度新增必填字段都判破坏（旧冻结规则只管顶层）。"""
    def post_spec(with_field):
        # with_field=False: meta 无 tag 字段；True: meta 新增 *必填* tag
        nested = (
            {"type": "object",
             "properties": {"tag": {"type": "string"}},
             "required": ["tag"]}
            if with_field else
            {"type": "object", "properties": {}}
        )
        props = {"meta": nested}
        return {
            "openapi": "3.1.0",
            "info": {"title": "Demo", "version": "1.0.0"},
            "paths": {"/items": {"post": {
                "operationId": "create", "tags": ["items"],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}},
                "responses": {"201": {"description": "created"}}}}},
            "components": {"schemas": {"Body": {
                "type": "object", "properties": props}}},
        }
    old, new = post_spec(False), post_spec(True)
    assert check_with_rules(parse_contract(old), parse_contract(new),
                            RULES_VERSION).compatible is False
    # 冻结旧规则：嵌套新增必填字段当时不判破坏
    assert check_with_rules(parse_contract(old), parse_contract(new),
                            RULES_VERSION_V1).compatible is True


def test_frozen_v1_response_required_to_optional_still_accepted():
    """冻结规则可回溯：同一演进在 2026.09.01 下结论仍是兼容。"""
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS), item_required=["id"])
    report = check_with_rules(parse_contract(old), parse_contract(new),
                              RULES_VERSION_V1)
    assert report.compatible is True
    assert any("必填变为可选" in a for a in report.accepted_changes)


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
    assert any("新增接口" in a for a in report.accepted_changes)
