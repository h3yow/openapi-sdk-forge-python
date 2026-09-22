"""SDK 代码生成测试：产物可编译、覆盖参数/请求体/多响应状态。"""

import json
import logging
import subprocess
import sys

import pytest

from sdk_forge.contract import parse_contract
from sdk_forge.generator import generate_sdk, render_sdk_zip
import zipfile
import io

logger = logging.getLogger("generator-tests")

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Shop", "version": "3.1.0"},
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
                    "200": {"description": "ok", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}},
                    "404": {"description": "missing"},
                },
            },
            "delete": {
                "operationId": "delete_item",
                "tags": ["items"],
                "parameters": [
                    {"name": "item_id", "in": "path", "required": True,
                     "schema": {"type": "integer"}},
                ],
                "responses": {"204": {"description": "gone"}},
            },
        },
        "/items": {
            "post": {
                "operationId": "create_item",
                "tags": ["items"],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ItemCreate"}}}},
                "responses": {
                    "201": {"description": "created", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}},
                },
            }
        },
    },
    "components": {"schemas": {
        "Item": {"type": "object", "required": ["id", "name"], "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"},
            "color": {"type": "string", "enum": ["red", "blue"]}}},
        "ItemCreate": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"}, "price": {"type": "number"}}},
    }},
}


@pytest.fixture(scope="module")
def files():
    logger.info("生成器契约输入:\n%s", json.dumps(SPEC, ensure_ascii=False, indent=2))
    ir = parse_contract(SPEC)
    result = generate_sdk(ir, package_name="shop_sdk")
    logger.info("生成依据/产物: %s", sorted(result))
    return result


def test_generated_files_are_python_compilable(files, tmp_path):
    for rel, content in files.items():
        if not rel.endswith(".py"):
            continue
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        compile(content, rel, "exec")  # 语法检查
    # 用子进程真正 import，避免污染当前解释器模块缓存
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.'); import shop_sdk; "
         "c = shop_sdk.Client('http://x'); assert hasattr(c, 'get_item')"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_generated_client_covers_params_body_and_statuses(files):
    client = files["shop_sdk/client.py"]
    # 路径参数 + 可选查询参数
    assert "def get_item(self, item_id: int, q: str = None)" in client
    assert "'item_id': item_id" in client
    assert "'q': q" in client
    # 请求体
    assert "def create_item(self, body: \"ItemCreate\")" in client
    # 方法与 HTTP 动作
    assert "self._request('GET', '/items/{item_id}'" in client
    assert "self._request('DELETE', '/items/{item_id}'" in client
    assert "self._request('POST', '/items'" in client
    # 模型
    models = files["shop_sdk/models.py"]
    assert "class Item:" in models and "class ItemCreate:" in models
    assert "id: int" in models and "name: str" in models
    # 内联枚举被提取
    assert "class ColorEnum(str, Enum):" in models
    # 元数据包含全部响应操作
    meta = json.loads(files["shop_sdk/metadata.json"])
    assert meta["version"] == "3.1.0"
    assert {o["name"] for o in meta["operations"]} == {
        "get_item", "delete_item", "create_item"}


def test_zip_bundle_roundtrip(files):
    blob = render_sdk_zip(files)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert names == set(files)
