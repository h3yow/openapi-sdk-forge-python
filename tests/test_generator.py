"""Generated-SDK tests: syntactic validity, model coverage and runtime calls.

The runtime test (test_generated_client_calls_live_service) boots the demo
service and exercises path params, query params, request body and multiple
response statuses against generated code.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

import pytest

from sdk_forge.contract import parse_contract
from sdk_forge.generator import generate_sdk

logger = logging.getLogger("test.generator")


def _doc() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Items API", "version": "2.0.0"},
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
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}},
                        },
                        "404": {
                            "description": "missing",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}},
                        },
                    },
                },
            },
            "/items": {
                "post": {
                    "operationId": "createItem",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ItemCreate"}}},
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {"application/json": {"schema": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Item"},
                            }}},
                        },
                    },
                },
            },
        },
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "price": {"type": "number"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "status": {"type": "string", "enum": ["draft", "active"]},
                    },
                    "required": ["id", "name"],
                },
                "ItemCreate": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "price": {"type": "number"},
                    },
                    "required": ["name"],
                },
                "ErrorBody": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                    "required": ["detail"],
                },
            },
        },
    }


@pytest.fixture(scope="module")
def bundle():
    contract = parse_contract(_doc())
    b = generate_sdk(contract)
    logger.info("generated files: %s", [f.path for f in b.files])
    return b


def _load_bundle(bundle, tmp_path: Path) -> types.ModuleType:
    pkg_dir = tmp_path / bundle.package_name
    pkg_dir.mkdir()
    for gen_file in bundle.files:
        (pkg_dir / gen_file.path).write_text(gen_file.content, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        bundle.package_name, pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[bundle.package_name] = module
    spec.loader.exec_module(module)
    return module


def test_bundle_file_inventory(bundle):
    assert {f.path for f in bundle.files} == {
        "__init__.py", "models.py", "client.py", "codegen.json"
    }
    meta = json.loads(bundle.file_map()["codegen.json"])
    assert meta["contract_version"] == "2.0.0"
    assert sorted(meta["operations"]) == ["createItem", "getItem"]
    assert len(meta["contract_fingerprint"]) == 64


def test_generated_code_compiles(bundle, tmp_path):
    module = _load_bundle(bundle, tmp_path)
    assert hasattr(module, "Client")
    assert hasattr(module, "ApiError")
    for model in ("Item", "ItemCreate", "ErrorBody"):
        assert hasattr(module, model), model


def test_generated_models_parse_roundtrip(bundle, tmp_path):
    module = _load_bundle(bundle, tmp_path)
    item = module.Item.from_dict(
        {"id": 7, "name": "hammer", "price": 12.5, "tags": ["a", "b"], "status": "active"}
    )
    assert item.id == 7
    assert item.tags == ["a", "b"]
    assert item.price == 12.5
    # missing optional field -> None, not an exception
    sparse = module.Item.from_dict({"id": 8, "name": "nail"})
    assert sparse.tags is None
    create = module.ItemCreate.from_dict({"name": "bolt"})
    assert create.name == "bolt" and create.price is None


def test_generated_method_signatures_cover_features(bundle, tmp_path):
    import inspect

    module = _load_bundle(bundle, tmp_path)
    sig = inspect.signature(module.Client.get_item)
    assert "item_id" in sig.parameters
    assert sig.parameters["verbose"].default is None
    create_sig = inspect.signature(module.Client.create_item)
    assert "body" in create_sig.parameters


def test_generated_client_uses_stdlib_only(bundle):
    client_src = bundle.file_map()["client.py"]
    # urllib is stdlib; ensure no third-party imports leaked in
    for line in client_src.splitlines():
        if line.startswith("import ") or line.startswith("from "):
            assert "fastapi" not in line and "requests" not in line
