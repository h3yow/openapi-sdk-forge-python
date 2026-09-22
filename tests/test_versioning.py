"""向后兼容性判定测试：破坏性变更为主，验证结论与字段级定位。"""

import copy
import json
import logging

import pytest

from sdk_forge.contract import parse_contract
from sdk_forge.versioning import (
    RULES_VERSION,
    check_backward_compatible,
    check_with_rules,
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


def test_response_required_to_optional_is_accepted():
    old = make_spec(item_props=dict(BASE_PROPS))
    new = make_spec(item_props=dict(BASE_PROPS), item_required=["id"])
    report = _judge(old, new)  # name 由必填变可选
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
