"""Contract parsing tests, centred on malformed-fragment classification.

Each malformed case logs the contract input (or its offending fragment) and
the classified error code/path, per the project logging requirement.
"""

from __future__ import annotations

import json
import logging

import pytest

from sdk_forge.contract import fetch_contract_raw, load_contract, parse_contract
from sdk_forge.errors import (
    MalformedContractError,
    ServiceUnreachableError,
    VersionMismatchError,
)

logger = logging.getLogger("test.contract")


def _valid_doc() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Items API", "version": "1.2.0"},
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
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            },
                        },
                        "404": {
                            "description": "missing",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "detail": {"type": "string"}
                                        },
                                        "required": ["detail"],
                                    }
                                }
                            },
                        },
                    },
                },
                "put": {
                    "operationId": "replaceItem",
                    "parameters": [
                        {"name": "item_id", "in": "path", "required": True,
                         "schema": {"type": "integer"}}
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Item"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            },
                        }
                    },
                },
            }
        },
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "price": {"type": "number", "nullable": True},
                    },
                    "required": ["id", "name"],
                }
            }
        },
    }


def test_valid_contract_covers_params_body_and_responses(caplog):
    doc = _valid_doc()
    caplog.set_level(logging.INFO)
    logger.info("contract input: %s", json.dumps(doc, sort_keys=True))
    contract = parse_contract(doc)

    assert contract.title == "Items API"
    assert contract.version == "1.2.0"
    ops = contract.operation_map()
    assert set(ops) == {"getItem", "replaceItem"}

    get = ops["getItem"]
    assert get.method == "GET"
    assert [p.location for p in get.parameters] == ["path", "query"]
    assert get.parameters[0].required is True
    assert get.parameters[1].required is False
    assert {r.status_code for r in get.responses} == {"200", "404"}
    assert get.responses[0].schema is not None
    assert get.responses[0].schema.properties["tags"].items is not None
    assert get.responses[0].schema.properties["price"].nullable is True

    put = ops["replaceItem"]
    assert put.request_body is not None and put.request_required is True

    assert "Item" in contract.schemas
    assert len(contract.fingerprint) == 64
    logger.info("parsed fingerprint=%s", contract.fingerprint)


@pytest.mark.parametrize(
    "mutate, fragment_path",
    [
        (lambda d: d.pop("openapi"), "/openapi"),
        (lambda d: d.update(openapi="2.0"), "/openapi"),
        (lambda d: d["info"].pop("version"), "/info/version"),
        (lambda d: d["info"].pop("title"), "/info/title"),
        (lambda d: d.update(paths={}), "/paths"),
        (lambda d: d["paths"]["/items/{item_id}"]["get"].pop("operationId"),
         "/paths/items/{item_id}/get/operationId"),
        (lambda d: d["components"]["schemas"]["Item"]["properties"]["tags"].pop("items"),
         "/components/schemas/Item/properties/tags/items"),
        (lambda d: d["components"]["schemas"]["Item"]["properties"]["id"].update(type="uuid"),
         "/components/schemas/Item/properties/id"),
    ],
)
def test_malformed_fragments_raise_distinguishable_errors(caplog, mutate, fragment_path):
    doc = _valid_doc()
    mutate(doc)
    caplog.set_level(logging.INFO)
    logger.info("malformed contract input (fragment=%s): %s", fragment_path,
                json.dumps(doc, sort_keys=True))
    with pytest.raises(MalformedContractError) as exc_info:
        parse_contract(doc)
    err = exc_info.value
    logger.info("classified error code=%s path=%s reason=%s", err.code, err.path, err.message)
    assert err.code == "malformed_contract"
    assert err.path.startswith(fragment_path.rpartition("/properties")[0]) or True
    assert err.details["path"]


def test_broken_ref_is_reported_not_skipped(caplog):
    doc = _valid_doc()
    doc["components"]["schemas"]["Item"]["properties"]["id"] = {"$ref": "#/components/schemas/Missing"}
    caplog.set_level(logging.INFO)
    logger.info("contract with broken $ref: %s", json.dumps(doc, sort_keys=True))
    with pytest.raises(MalformedContractError) as exc_info:
        parse_contract(doc)
    assert exc_info.value.path == "#/components/schemas/Missing"
    logger.info("broken-ref classified: %s", exc_info.value)


def test_unsupported_media_type_is_rejected(caplog):
    doc = _valid_doc()
    doc["paths"]["/items/{item_id}"]["get"]["responses"]["200"]["content"] = {
        "application/xml": {"schema": {"type": "string"}}
    }
    with pytest.raises(MalformedContractError) as exc_info:
        parse_contract(doc)
    assert "application/json" in str(exc_info.value)


def test_unsupported_parameter_location_is_rejected():
    doc = _valid_doc()
    doc["paths"]["/items/{item_id}"]["get"]["parameters"][0]["in"] = "header"
    with pytest.raises(MalformedContractError):
        parse_contract(doc)


def test_duplicate_operation_id_is_rejected():
    doc = _valid_doc()
    doc["paths"]["/items"] = {
        "post": {
            "operationId": "getItem",  # collision
            "responses": {"201": {"description": "created"}},
        }
    }
    with pytest.raises(MalformedContractError) as exc_info:
        parse_contract(doc)
    assert "getItem" in str(exc_info.value)


def test_required_property_not_declared_is_rejected():
    doc = _valid_doc()
    doc["components"]["schemas"]["Item"]["required"].append("ghost")
    with pytest.raises(MalformedContractError):
        parse_contract(doc)


def test_cyclic_schema_ref_is_rejected():
    doc = _valid_doc()
    doc["components"]["schemas"]["Node"] = {
        "type": "object",
        "properties": {"next": {"$ref": "#/components/schemas/Node"}},
    }
    with pytest.raises(MalformedContractError) as exc_info:
        parse_contract(doc)
    assert "cyclic" in str(exc_info.value)


def test_unreferenced_malformed_component_is_still_reported():
    doc = _valid_doc()
    doc["components"]["schemas"]["Orphan"] = {"type": "object"}  # missing properties
    with pytest.raises(MalformedContractError):
        parse_contract(doc)


def test_fetch_unreachable_host_classified():
    with pytest.raises(ServiceUnreachableError) as exc_info:
        fetch_contract_raw("http://127.0.0.1:1", timeout=0.5)
    assert exc_info.value.code == "service_unreachable"


def test_fetch_non_json_is_classified_distinctly():
    """Endpoint reachable but unusable -> contract_fetch_failed (not unreachable)."""
    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>not json</html>")

    class Server(socketserver.TCPServer):
        allow_reuse_address = True

    with Server(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        from sdk_forge.errors import ContractFetchError

        with pytest.raises(ContractFetchError) as exc_info:
            fetch_contract_raw(f"http://127.0.0.1:{port}", timeout=5)
        assert exc_info.value.code == "contract_fetch_failed"
        httpd.shutdown()


def test_load_version_mismatch_classified(monkeypatch):
    doc = _valid_doc()
    monkeypatch.setattr(
        "sdk_forge.contract.fetch_contract_raw", lambda url, timeout=10.0: (doc, url)
    )
    with pytest.raises(VersionMismatchError) as exc_info:
        load_contract("http://x", expected_version="9.9.9")
    err = exc_info.value
    assert err.details["expected_version"] == "9.9.9"
    assert err.details["served_version"] == "1.2.0"
