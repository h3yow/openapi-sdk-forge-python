"""契约解析与获取测试：重点覆盖畸形/缺失片段的可区分错误。"""

import json
import logging
import socket

import pytest

from sdk_forge.contract import fetch_openapi, parse_contract
from sdk_forge.errors import (
    ContractFetchError,
    MalformedContractError,
    MissingContractFragmentError,
    ServiceUnreachableError,
)

logger = logging.getLogger("contract-tests")

ITEM_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Demo", "version": "1.0.0"},
    "paths": {
        "/items/{item_id}": {
            "get": {
                "operationId": "get_item",
                "tags": ["items"],
                "parameters": [
                    {"name": "item_id", "in": "path", "required": True,
                     "schema": {"type": "integer"}},
                    {"name": "q", "in": "query", "required": False,
                     "schema": {"type": "string"}},
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
                    "404": {"description": "missing"},
                },
            }
        },
        "/items": {
            "post": {
                "operationId": "create_item",
                "tags": ["items"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ItemCreate"}
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Item"}
                            }
                        },
                    }
                },
            }
        },
    },
    "components": {
        "schemas": {
            "Item": {
                "type": "object",
                "required": ["id", "name"],
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
            "ItemCreate": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        }
    },
}


def test_parse_valid_contract_covers_params_body_responses():
    logger.info("契约输入:\n%s", json.dumps(ITEM_SPEC, ensure_ascii=False, indent=2))
    ir = parse_contract(ITEM_SPEC)
    assert ir.version == "1.0.0"
    assert {op.key for op in ir.operations} == {
        "GET /items/{item_id}",
        "POST /items",
    }
    get_op = next(o for o in ir.operations if o.method == "GET")
    assert [p.name for p in get_op.parameters] == ["item_id", "q"]
    assert get_op.parameters[0].python_type == "int"
    assert [r.status for r in get_op.responses] == ["200", "404"]
    post_op = next(o for o in ir.operations if o.method == "POST")
    assert post_op.body is not None and post_op.body.type_name == "ItemCreate"
    assert post_op.responses[0].status == "201"
    logger.info("解析依据: operations=%d models=%s",
                len(ir.operations), sorted(ir.models))


def test_dangling_ref_is_distinct_error():
    spec = json.loads(json.dumps(ITEM_SPEC))
    spec["components"]["schemas"]["Item"]["properties"]["id"] = {
        "$ref": "#/components/schemas/Ghost"
    }
    logger.info("畸形契约输入(悬空ref):\n%s",
                json.dumps(spec, ensure_ascii=False))
    with pytest.raises(MissingContractFragmentError) as exc:
        parse_contract(spec)
    assert "Ghost" in str(exc.value)
    assert exc.value.fragment == "#/components/schemas/Ghost"


def test_unsupported_type_is_malformed():
    spec = json.loads(json.dumps(ITEM_SPEC))
    spec["components"]["schemas"]["ItemCreate"]["properties"]["name"] = {
        "type": "weird-type"
    }
    logger.info("畸形契约输入(未知type):\n%s",
                json.dumps(spec, ensure_ascii=False))
    with pytest.raises(MalformedContractError, match="weird-type"):
        parse_contract(spec)


def test_path_param_not_required_is_malformed():
    spec = json.loads(json.dumps(ITEM_SPEC))
    spec["paths"]["/items/{item_id}"]["get"]["parameters"][0]["required"] = False
    with pytest.raises(MalformedContractError, match="路径参数"):
        parse_contract(spec)


def test_missing_responses_is_malformed():
    spec = json.loads(json.dumps(ITEM_SPEC))
    del spec["paths"]["/items"]["post"]["responses"]
    with pytest.raises(MalformedContractError, match="响应"):
        parse_contract(spec)


def test_external_ref_rejected():
    spec = json.loads(json.dumps(ITEM_SPEC))
    spec["paths"]["/items"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"] = {"$ref": "https://example.com/schemas/X.json"}
    with pytest.raises(MalformedContractError, match="文档内"):
        parse_contract(spec)


def test_empty_paths_rejected():
    with pytest.raises(MalformedContractError, match="paths"):
        parse_contract({"openapi": "3.1.0", "info": {"title": "x"}, "paths": {}})


def test_wrong_openapi_version_rejected():
    with pytest.raises(MalformedContractError, match="OpenAPI 3.x"):
        parse_contract({"openapi": "2.0", "info": {"title": "x"}, "paths": {}})


def test_fetch_unreachable_does_not_hang():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(ServiceUnreachableError, match="无法连接"):
        fetch_openapi(f"http://127.0.0.1:{free_port}", timeout=1.0)


def test_fetch_non_json_is_contract_fetch_error():
    # 用一个永不占用端口的方式无法构造非 JSON；改用本地 http 服务
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"this is not json")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ContractFetchError, match="合法 JSON"):
            fetch_openapi(f"http://127.0.0.1:{server.server_port}", timeout=2.0)
    finally:
        server.shutdown()


def test_cookie_parameter_rejected():
    spec = json.loads(json.dumps(ITEM_SPEC))
    spec["paths"]["/items/{item_id}"]["get"]["parameters"].append({
        "name": "session", "in": "cookie", "required": False,
        "schema": {"type": "string"}})
    logger.info("畸形契约输入(cookie参数):\n%s", json.dumps(spec, ensure_ascii=False))
    with pytest.raises(MalformedContractError, match="path/query/header"):
        parse_contract(spec)


def test_non_json_media_type_rejected():
    spec = json.loads(json.dumps(ITEM_SPEC))
    spec["paths"]["/items"]["post"]["requestBody"]["content"] = {
        "application/xml": {"schema": {"type": "object"}}}
    with pytest.raises(MalformedContractError, match="JSON"):
        parse_contract(spec)


def test_empty_enum_rejected():
    spec = json.loads(json.dumps(ITEM_SPEC))
    spec["components"]["schemas"]["Item"]["properties"]["name"] = {
        "type": "string", "enum": []}
    with pytest.raises(MalformedContractError, match="enum"):
        parse_contract(spec)


def test_array_without_items_rejected():
    spec = json.loads(json.dumps(ITEM_SPEC))
    spec["components"]["schemas"]["Item"]["properties"]["tags"] = {"type": "array"}
    with pytest.raises(MalformedContractError, match="items"):
        parse_contract(spec)
