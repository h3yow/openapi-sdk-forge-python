"""Backward-compatibility tests, centred on breaking contract evolution.

Every case logs the contract inputs (fingerprints + operation inventory) and
the rule-by-rule decision basis emitted by the checker.
"""

from __future__ import annotations

import copy
import logging

import pytest

from sdk_forge.compat import (
    RULE_VERSION,
    RULE_VERSION_V1,
    Change,
    Severity,
    check_backward_compatible,
)
from sdk_forge.contract import parse_contract

logger = logging.getLogger("test.compat")


# --------------------------------------------------------------------------- #
# Fixture contracts
# --------------------------------------------------------------------------- #

def _item_schema(extra_props=None, required=("id", "name")):
    props = {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "price": {"type": "number"},
        "status": {"type": "string", "enum": ["draft", "active"]},
    }
    if extra_props:
        props.update(extra_props)
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
    }


def _doc(version: str, item_schema: dict, response_schema: dict | None = None):
    response_schema = response_schema or item_schema
    response_ref = "#/components/schemas/Item" if response_schema is item_schema else "#/components/schemas/ItemView"
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "Items API", "version": version},
        "paths": {
            "/items/{item_id}": {
                "get": {
                    "operationId": "getItem",
                    "parameters": [
                        {"name": "item_id", "in": "path", "required": True,
                         "schema": {"type": "integer"}},
                        {"name": "verbose", "in": "query", "required": False,
                         "schema": {"type": "boolean"}},
                    ],
                    "responses": {
                        "200": {"description": "ok",
                                "content": {"application/json": {"schema": {"$ref": response_ref}}}},
                        "404": {"description": "missing",
                                "content": {"application/json": {"schema": {"type": "object",
                                            "properties": {"detail": {"type": "string"}},
                                            "required": ["detail"]}}}},
                    },
                },
                "post": {
                    "operationId": "createItem",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}},
                    },
                    "responses": {
                        "201": {"description": "created",
                                "content": {"application/json": {"schema": {"$ref": response_ref}}}},
                        "400": {"description": "bad"},
                    },
                },
            }
        },
        "components": {"schemas": {"Item": item_schema}},
    }
    if response_schema is not item_schema:
        doc["components"]["schemas"]["ItemView"] = response_schema
    return doc


@pytest.fixture
def v1_contract():
    return parse_contract(_doc("1.0.0", _item_schema()))


def _evolve(item_schema=None, version="1.1.0", mutate_doc=None, response_schema=None):
    doc = _doc(version, item_schema or _item_schema(), response_schema=response_schema)
    if mutate_doc:
        mutate_doc(doc)
    return parse_contract(doc)


def _log_basis(caplog, report):
    caplog.set_level(logging.INFO)
    for change in report.changes:
        logger.info("decision rule=%s severity=%s loc=%s :: %s",
                    change.rule_id, change.severity.value, change.location, change.message)
    logger.info("verdict compatible=%s rule_version=%s", report.compatible, report.rule_version)


# --------------------------------------------------------------------------- #
# Accepted evolution
# --------------------------------------------------------------------------- #

def test_add_optional_response_field_is_accepted(v1_contract, caplog):
    new = _evolve(_item_schema({"description": {"type": "string"}}))
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is True
    assert any(c.rule_id == "R002-optional-field-added" and "description" in c.location
               for c in report.changes)


def test_add_optional_request_field_is_accepted(v1_contract, caplog):
    schema = _item_schema({"note": {"type": "string"}}, required=("id", "name"))
    new = _evolve(schema)
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is True


def test_integer_to_number_widening_is_accepted(v1_contract, caplog):
    # id: integer -> number in both request and response (widening)
    new = _evolve(_item_schema({"id": {"type": "number"}}))
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is True
    assert any(c.rule_id == "R004-type-widened" for c in report.changes)


def test_add_enum_value_accepted_under_current_rules(v1_contract, caplog):
    schema = _item_schema({"status": {"type": "string", "enum": ["draft", "active", "archived"]}})
    new = _evolve(schema)
    report = check_backward_compatible(v1_contract, new, rule_version=RULE_VERSION)
    _log_basis(caplog, report)
    assert report.compatible is True
    assert any(c.rule_id == "R205-enum-widened" for c in report.changes)


def test_add_operation_and_status_code_accepted(v1_contract, caplog):
    def mutate(doc):
        doc["paths"]["/items"] = {
            "get": {"operationId": "listItems",
                    "responses": {"200": {"description": "ok"}}}
        }
        doc["paths"]["/items/{item_id}"]["get"]["responses"]["503"] = {"description": "busy"}
    new = _evolve(mutate_doc=mutate)
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is True
    assert any(c.rule_id == "R001-operation-added" for c in report.changes)
    assert any(c.rule_id == "R008-response-status-added" for c in report.changes)


def test_response_field_becomes_guaranteed_is_accepted(v1_contract, caplog):
    # price was optional in the response, now required in the response only
    # (request Item schema unchanged) -> stronger guarantee, accepted.
    new = _evolve(response_schema=_item_schema(required=("id", "name", "price")))
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is True
    assert any(c.rule_id == "R003-required-strengthened" for c in report.changes)


def test_response_field_becomes_optional_is_rejected(v1_contract, caplog):
    new = _evolve(response_schema=_item_schema(required=("id",)))
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert any(c.rule_id == "R003-required-weakened" and "name" in c.location
               for c in report.breaking())


# --------------------------------------------------------------------------- #
# Breaking evolution
# --------------------------------------------------------------------------- #

def test_remove_field_is_rejected_with_attribution(v1_contract, caplog):
    schema = _item_schema()
    del schema["properties"]["price"]
    new = _evolve(schema)
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is False
    breaking = report.breaking()
    assert all(c.severity == Severity.BREAKING for c in breaking)
    removed = [c for c in breaking if c.rule_id == "R002-field-removed" and "price" in c.location]
    assert removed, breaking
    assert all("operation:" in c.location for c in breaking)


def test_request_field_becomes_required_is_rejected(v1_contract, caplog):
    # createItem request body: price becomes required (必填化)
    new = _evolve(_item_schema(required=("id", "name", "price")))
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is False
    assert any(c.rule_id == "R003-required-tightened" and "operation:createItem" in c.location
               for c in report.breaking())


def test_number_to_integer_narrowing_is_rejected(v1_contract, caplog):
    new = _evolve(_item_schema({"price": {"type": "integer"}}))
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is False
    assert any(c.rule_id == "R004-type-narrowed" and "price" in c.location
               for c in report.breaking())


def test_string_to_boolean_type_change_is_rejected(v1_contract, caplog):
    new = _evolve(_item_schema({"name": {"type": "boolean"}}))
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is False
    assert any("name" in c.location for c in report.breaking())


def test_remove_enum_value_is_rejected_under_both_rule_sets(v1_contract, caplog):
    schema = _item_schema({"status": {"type": "string", "enum": ["draft"]}})
    new = _evolve(schema)
    for rv in (RULE_VERSION, RULE_VERSION_V1):
        report = check_backward_compatible(v1_contract, new, rule_version=rv)
        _log_basis(caplog, report)
        assert report.compatible is False
        assert report.rule_version == rv


def test_remove_operation_is_rejected_with_operation_name(v1_contract, caplog):
    def mutate(doc):
        del doc["paths"]["/items/{item_id}"]["post"]
    new = _evolve(mutate_doc=mutate)
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert report.compatible is False
    assert any(c.rule_id == "R001-operation-removed" and "createItem" in c.location
               for c in report.breaking())


def test_optional_query_param_becomes_required_is_rejected(v1_contract, caplog):
    def mutate(doc):
        get = doc["paths"]["/items/{item_id}"]["get"]
        get["parameters"][1]["required"] = True
    new = _evolve(mutate_doc=mutate)
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert any(c.rule_id == "R006-parameter-made-required" and "verbose" in c.location
               for c in report.breaking())


def test_response_field_becomes_nullable_is_rejected(v1_contract, caplog):
    new = _evolve(_item_schema({"name": {"type": "string", "nullable": True}}))
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert any(c.rule_id == "R007-nullable-weakened" and "operation:getItem" in c.location
               for c in report.breaking())


def test_remove_response_status_is_rejected(v1_contract, caplog):
    def mutate(doc):
        del doc["paths"]["/items/{item_id}"]["get"]["responses"]["404"]
    new = _evolve(mutate_doc=mutate)
    report = check_backward_compatible(v1_contract, new)
    _log_basis(caplog, report)
    assert any(c.rule_id == "R008-response-status-removed" and "404" in c.location
               for c in report.breaking())


# --------------------------------------------------------------------------- #
# Rule-version reproducibility
# --------------------------------------------------------------------------- #

def test_enum_evolution_verdict_reproduces_per_rule_version(v1_contract, caplog):
    """Adding an enum value: rejected by frozen v1, accepted by v2."""
    schema = _item_schema({"status": {"type": "string", "enum": ["draft", "active", "archived"]}})
    new = _evolve(schema)

    old_report = check_backward_compatible(v1_contract, new, rule_version=RULE_VERSION_V1)
    new_report = check_backward_compatible(v1_contract, new, rule_version=RULE_VERSION)
    _log_basis(caplog, old_report)
    _log_basis(caplog, new_report)

    assert old_report.compatible is False
    assert new_report.compatible is True
    assert any(c.rule_id == "R105-enum-changed" for c in old_report.breaking())
    assert any(c.rule_id == "R205-enum-widened" for c in new_report.changes)
    # fingerprints make the exact pair of compared inputs reproducible
    assert old_report.old_fingerprint == new_report.old_fingerprint == v1_contract.fingerprint
    assert old_report.new_fingerprint == new_report.new_fingerprint == new.fingerprint


def test_report_dict_is_json_serializable(v1_contract, caplog):
    import json
    new = _evolve(_item_schema({"description": {"type": "string"}}))
    report = check_backward_compatible(v1_contract, new)
    payload = json.dumps(report.to_dict())
    assert "R002-optional-field-added" in payload


def test_unknown_rule_version_raises(v1_contract):
    with pytest.raises(ValueError):
        check_backward_compatible(v1_contract, v1_contract, rule_version="rules-1999.01")


def test_identical_contracts_are_compatible(v1_contract, caplog):
    report = check_backward_compatible(v1_contract, parse_contract(copy.deepcopy(_doc("1.0.0", _item_schema()))))
    _log_basis(caplog, report)
    assert report.compatible is True
    assert report.changes == []
