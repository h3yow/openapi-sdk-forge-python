"""契约获取与解析：OpenAPI JSON -> ContractIR。

受支持的契约范围见 README。任何缺失/悬空片段或不受支持的结构都会抛出
``MalformedContractError`` / ``MissingContractFragmentError``，绝不静默跳过。
"""

from __future__ import annotations

import json
import keyword
import re
import urllib.error
import urllib.request
from typing import Any

from .errors import (
    MalformedContractError,
    MissingContractFragmentError,
    ServiceUnreachableError,
)
from .models import (
    BodyInfo,
    ContractIR,
    ModelField,
    Operation,
    Parameter,
    ResponseInfo,
    SchemaModel,
)

_OPENAPI_PATH = "/openapi.json"

# OpenAPI/JSON Schema -> Python 类型
_SCALAR_TYPES: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
}

_FORMATTED_TYPES: dict[tuple[str, str], str] = {
    ("string", "date-time"): "datetime",
    ("string", "date"): "datetime",
    ("string", "uuid"): "str",
    ("string", "binary"): "bytes",
    ("integer", "int32"): "int",
    ("integer", "int64"): "int",
    ("number", "float"): "float",
    ("number", "double"): "float",
}


def _slug(name: str) -> str:
    """把任意字符串转为合法 Python 标识符（蛇形）。"""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    if not s:
        s = "field"
    if s[0].isdigit():
        s = f"_{s}"
    if keyword.iskeyword(s):
        s += "_"
    return s


def _pascal(name: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", str(name))
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not out:
        out = "Model"
    if out[0].isdigit():
        out = f"M{out}"
    return out


class _Resolver:
    """解析文档内的本地 $ref，并保留解析上下文用于报错。"""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec

    def resolve(self, ref: str, context: str) -> Any:
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise MalformedContractError(
                f"仅支持文档内 $ref（以 '#/' 开头），收到: {ref!r} (位于 {context})"
            )
        node: Any = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise MissingContractFragmentError(ref, context=context)
            node = node[part]
        return node


class _Parser:
    def __init__(
        self, spec: dict[str, Any], *, include_tags: set[str] | None = None
    ) -> None:
        self.spec = spec
        self.include_tags = include_tags
        self.resolver = _Resolver(spec)
        self.models: dict[str, SchemaModel] = {}
        self._component_schemas: dict[str, dict] = {}
        self._built: set[str] = set()
        self._inline_counter = 0
        self._op_names: set[str] = set()

    # ---------- 基础工具 ----------

    def _register_inline_model(self, base: str, schema: dict) -> str:
        """注册内联对象模型，按结构去重。"""
        model = self._build_model(base, schema, context=f"内联模型 {base}")
        existing = self.models.get(model.name)
        if existing is not None and existing.raw_schema == schema:
            return model.name
        if model.name in self.models:
            i = 2
            while f"{model.name}{i}" in self.models:
                i += 1
            model.name = f"{model.name}{i}"
        self.models[model.name] = model
        return model.name

    def _build_model(self, name: str, schema: dict, *, context: str) -> SchemaModel:
        """把一个 object schema 转成 SchemaModel（不放入注册表）。"""
        schema = self._deref(schema, context)
        if schema.get("type", "object") != "object" or "properties" not in schema:
            raise MalformedContractError(
                f"期望对象 schema（含 properties），收到: {json.dumps(schema, ensure_ascii=False)[:200]} "
                f"(位于 {context})"
            )
        required = set(schema.get("required", []))
        fields: list[ModelField] = []
        for fname, fschema in schema["properties"].items():
            fctx = f"{context}.{fname}"
            if not isinstance(fschema, dict):
                raise MalformedContractError(f"字段 {fname!r} 必须是 schema 对象 (位于 {fctx})")
            resolved = self._deref(fschema, fctx)
            fields.append(
                ModelField(
                    name=fname,
                    python_type=self._type_expr(fschema, fctx, base=_pascal(fname)),
                    required=fname in required,
                    description=resolved.get("description", ""),
                    schema_raw=resolved,
                )
            )
        return SchemaModel(
            name=name,
            fields=fields,
            description=schema.get("description", ""),
            raw_schema=schema,
        )

    def _deref(self, schema: Any, context: str) -> dict:
        if not isinstance(schema, dict):
            raise MalformedContractError(f"期望 schema 为对象 (位于 {context})")
        if "$ref" in schema:
            resolved = self.resolver.resolve(schema["$ref"], context)
            if not isinstance(resolved, dict):
                raise MissingContractFragmentError(
                    schema["$ref"], context=context
                )
            return resolved
        return schema

    def _ref_name(self, schema: Any) -> str | None:
        """若 schema 是指向 components.schemas/<Name> 的 $ref，返回 <Name>。"""
        if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
            ref = schema["$ref"]
            prefix = "#/components/schemas/"
            if ref.startswith(prefix):
                name = ref[len(prefix):]
                if "/" not in name:
                    return name
        return None

    def _type_expr(self, schema: dict, context: str, *, base: str) -> str:
        """把 schema 渲染成 Python 类型表达式；遇到对象/枚举会登记模型。"""
        named = self._ref_name(schema)
        if named is not None:
            if named not in self.models:
                raise MissingContractFragmentError(
                    f"#/components/schemas/{named}", context=context
                )
            if named not in self._built:
                self._build_named(named)
            return named
        schema = self._deref(schema, context)

        # OpenAPI 3.1 anyOf/oneOf
        for combiner in ("anyOf", "oneOf"):
            if combiner in schema:
                members = schema[combiner]
                if not isinstance(members, list) or not members:
                    raise MalformedContractError(
                        f"{combiner} 必须是非空数组 (位于 {context})"
            )
                types: list[str] = []
                nullable = False
                for i, m in enumerate(members):
                    m = self._deref(m, f"{context}[{i}]")
                    if m.get("type") == "null":
                        nullable = True
                        continue
                    types.append(
                        self._type_expr(m, f"{context}[{i}]", base=f"{base}Variant{i+1}")
                    )
                if not types:
                    raise MalformedContractError(
                        f"{combiner} 不允许只包含 null (位于 {context})"
                    )
                expr = " | ".join(dict.fromkeys(types))
                return f"{expr} | None" if nullable else expr

        if "allOf" in schema:
            raise MalformedContractError(
                f"暂不支持 allOf 组合 schema (位于 {context})"
            )

        stype = schema.get("type")
        if stype is None:
            # 无 type 的 schema 视为任意值
            return "object"

        if stype == "array":
            items = schema.get("items")
            if not isinstance(items, dict):
                raise MalformedContractError(
                    f"array schema 缺少 items (位于 {context})"
                )
            inner = self._type_expr(items, f"{context}[]", base=base)
            return f"list[{inner}]"

        if stype == "object":
            if "properties" in schema:
                return self._register_inline_model(base, schema)
            if "additionalProperties" in schema:
                ap = schema["additionalProperties"]
                if ap is False:
                    return "dict[str, object]"
                if isinstance(ap, dict):
                    vt = self._type_expr(ap, f"{context}.<value>", base=f"{base}Value")
                    return f"dict[str, {vt}]"
            return "dict[str, object]"

        if "enum" in schema and stype == "string":
            # 内联枚举：登记为具名 Enum 模型
            name = self._unique_enum_name(base)
            values = schema["enum"]
            if not isinstance(values, list) or not values:
                raise MalformedContractError(f"enum 必须是非空数组 (位于 {context})")
            self.models[name] = SchemaModel(
                name=name,
                is_enum=True,
                enum_values=[str(v) for v in values],
                raw_schema=schema,
            )
            return name

        if stype in _SCALAR_TYPES:
            fmt = schema.get("format")
            return _FORMATTED_TYPES.get((stype, fmt), _SCALAR_TYPES[stype])

        raise MalformedContractError(
            f"不支持的 schema type: {stype!r} (位于 {context})"
        )

    def _unique_enum_name(self, base: str) -> str:
        name = _pascal(base)
        if not name.endswith("Enum"):
            name += "Enum"
        if name in self.models:
            i = 2
            while f"{name}{i}" in self.models:
                i += 1
            name = f"{name}{i}"
        return name

    # ---------- components 预登记 ----------

    def _load_components(self) -> None:
        schemas = self.spec.get("components", {}).get("schemas", {})
        if not isinstance(schemas, dict):
            raise MalformedContractError("components.schemas 必须是对象")
        # 第一遍：登记全部名称（解决前向引用）
        for name, schema in schemas.items():
            ctx = f"#/components/schemas/{name}"
            if not isinstance(schema, dict):
                raise MalformedContractError(f"{ctx} 必须是 schema 对象")
            self._component_schemas[name] = schema
            if schema.get("enum") and schema.get("type") == "string":
                values = schema["enum"]
                if not isinstance(values, list) or not values:
                    raise MalformedContractError(f"{ctx} 的 enum 必须是非空数组")
                self.models[name] = SchemaModel(
                    name=name,
                    is_enum=True,
                    enum_values=[str(v) for v in values],
                    description=schema.get("description", ""),
                    raw_schema=schema,
                )
                self._built.add(name)
            elif schema.get("type", "object") == "object":
                # 占位，构建在第二遍惰性完成
                self.models[name] = SchemaModel(name=name)
            else:
                raise MalformedContractError(
                    f"仅支持 object 与 string enum 的具名 schema，{ctx} 的 type={schema.get('type')!r}"
                )
        if self.include_tags is None:
            # 全量解析：强制构建全部对象模型（暴露悬空引用等畸形问题）
            for name in list(schemas):
                if name not in self._built:
                    self._build_named(name)

    def _build_named(self, name: str) -> SchemaModel:
        if name in self._built:
            return self.models[name]
        schema = self._component_schemas[name]
        model = self._build_model(
            name, schema, context=f"#/components/schemas/{name}"
        )
        self.models[name] = model
        self._built.add(name)
        return model

    # ---------- operation 解析 ----------

    def _parse_parameter(self, raw: dict, context: str) -> Parameter:
        raw = self._deref(raw, context)
        pname = raw.get("name")
        location = raw.get("in")
        if not pname or not isinstance(pname, str):
            raise MalformedContractError(f"参数缺少 name (位于 {context})")
        if location not in ("path", "query", "header"):
            raise MalformedContractError(
                f"参数 {pname!r} 的 in={location!r} 不受支持（仅支持 path/query/header）"
                f" (位于 {context})"
            )
        schema_raw = raw.get("schema")
        if not isinstance(schema_raw, dict):
            raise MalformedContractError(f"参数 {pname!r} 缺少 schema (位于 {context})")
        required = bool(raw.get("required", location == "path"))
        if location == "path" and not required:
            raise MalformedContractError(
                f"路径参数 {pname!r} 必须 required=true (位于 {context})"
            )
        return Parameter(
            name=pname,
            location=location,
            required=required,
            python_type=self._type_expr(
                schema_raw, f"{context}.schema", base=_pascal(pname)
            ),
            var_name=_slug(pname),
            schema_raw=self._deref(schema_raw, f"{context}.schema"),
        )

    def _media_schema(self, content: dict, context: str) -> tuple[dict, str]:
        if not isinstance(content, dict) or not content:
            raise MalformedContractError(f"content 必须是非空对象 (位于 {context})")
        media_type = "application/json"
        entry = content.get(media_type)
        if entry is None:
            # 取第一个媒体类型，但只接受 JSON 系列
            media_type = next(iter(content))
            if "json" not in media_type:
                raise MalformedContractError(
                    f"仅支持 JSON 请求/响应媒体类型，收到 {media_type!r} (位于 {context})"
                )
            entry = content[media_type]
        schema = entry.get("schema") if isinstance(entry, dict) else None
        if not isinstance(schema, dict):
            raise MalformedContractError(f"媒体项缺少 schema (位于 {context})")
        return schema, media_type

    def _parse_operation(
        self, method: str, path: str, raw: dict, context: str
    ) -> Operation:
        op_id = raw.get("operationId")
        if not op_id or not isinstance(op_id, str):
            op_id = f"{method.lower()}_{_slug(path)}"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", op_id):
            op_id = _slug(op_id)
        python_name = op_id
        i = 2
        base_name = python_name
        while python_name in self._op_names:
            python_name = f"{base_name}_{i}"
            i += 1
        self._op_names.add(python_name)

        params: list[Parameter] = []
        for j, praw in enumerate(raw.get("parameters", [])):
            params.append(
                self._parse_parameter(praw, f"{context}.parameters[{j}]")
            )

        body: BodyInfo | None = None
        if "requestBody" in raw:
            rb = self._deref(raw["requestBody"], f"{context}.requestBody")
            schema, media_type = self._media_schema(
                rb.get("content", {}), f"{context}.requestBody"
            )
            required = bool(rb.get("required", False))
            named = self._ref_name(schema)
            if named is not None:
                if named not in self.models:
                    raise MissingContractFragmentError(
                        f"#/components/schemas/{named}",
                        context=f"{context}.requestBody",
                    )
                if named not in self._built:
                    self._build_named(named)
                type_name: str | None = named
            elif self._deref(schema, context).get("type", "object") == "object" and "properties" in self._deref(schema, context):
                type_name = self._register_inline_model(
                    f"{_pascal(python_name)}Request", schema
                )
            else:
                type_name = self._type_expr(
                    schema,
                    f"{context}.requestBody",
                    base=f"{_pascal(python_name)}Request",
                )
            body = BodyInfo(
                required=required,
                type_name=type_name,
                content_type=media_type,
                schema_raw=self._deref(schema, context),
            )

        responses_raw = raw.get("responses")
        if not isinstance(responses_raw, dict) or not responses_raw:
            raise MalformedContractError(f"{context} 必须声明至少一个响应")
        responses: list[ResponseInfo] = []
        for status, resp in responses_raw.items():
            rctx = f"{context}.responses[{status}]"
            resp = self._deref(resp, rctx)
            type_name = None
            schema: dict = {}
            content = resp.get("content")
            if isinstance(content, dict) and content:
                schema, _ = self._media_schema(content, rctx)
                type_name = self._type_expr(
                    schema, rctx, base=f"{_pascal(python_name)}Resp{_pascal(str(status))}"
                )
            responses.append(
                ResponseInfo(
                    status=str(status),
                    type_name=type_name,
                    description=resp.get("description", ""),
                    schema_raw=self._deref(schema, rctx) if schema else {},
                )
            )

        tags = raw.get("tags") or ["default"]
        return Operation(
            operation_id=op_id,
            method=method.upper(),
            path=path,
            tag=str(tags[0]),
            parameters=params,
            body=body,
            responses=responses,
            python_name=python_name,
        )

    def parse(self) -> ContractIR:
        spec = self.spec
        if not isinstance(spec, dict):
            raise MalformedContractError("契约根节点必须是 JSON 对象")
        version_field = spec.get("openapi")
        if not isinstance(version_field, str) or not version_field.startswith("3."):
            raise MalformedContractError(
                f"仅支持 OpenAPI 3.x 契约，收到 openapi={version_field!r}"
            )
        info = spec.get("info")
        if not isinstance(info, dict) or not isinstance(info.get("title"), str):
            raise MalformedContractError("契约缺少 info.title")
        contract_version = info.get("version")
        if contract_version is not None and not isinstance(contract_version, str):
            raise MalformedContractError("info.version 必须是字符串")

        self._load_components()

        paths = spec.get("paths")
        if not isinstance(paths, dict) or not paths:
            raise MalformedContractError("契约必须声明非空的 paths")

        operations: list[Operation] = []
        for path, item in paths.items():
            if not isinstance(path, str) or not path.startswith("/"):
                raise MalformedContractError(f"非法 path: {path!r}")
            item = self._deref(item, f"paths[{path}]")
            path_level_params = item.get("parameters", [])
            if not isinstance(path_level_params, list):
                raise MalformedContractError(f"paths[{path}].parameters 必须是数组")
            for method in ("get", "post", "put", "patch", "delete"):
                if method not in item:
                    continue
                raw_op = item[method]
                ctx = f"{method.upper()} {path}"
                if not isinstance(raw_op, dict):
                    raise MalformedContractError(f"{ctx} 必须是操作对象")
                merged = dict(raw_op)
                if path_level_params:
                    merged["parameters"] = path_level_params + raw_op.get(
                        "parameters", []
                    )
                op_tags = merged.get("tags") or ["default"]
                if self.include_tags is not None and not (
                    set(op_tags) & self.include_tags
                ):
                    continue
                operations.append(self._parse_operation(method, path, merged, ctx))

        if self.include_tags is not None:
            self._prune_unused_models(operations)

        return ContractIR(
            title=info["title"],
            version=contract_version,
            operations=operations,
            models=self.models,
            raw=spec,
        )

    def _prune_unused_models(self, operations: list[Operation]) -> None:
        """tag 过滤后移除未被纳入操作（传递）引用的组件模型与内联模型。"""
        import re as _re

        used: set[str] = set()

        def collect_from_type(expr: str) -> None:
            for token in _re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
                if token in self.models and token not in used:
                    used.add(token)
                    m = self.models[token]
                    for f in m.fields:
                        collect_from_type(f.python_type)

        for op in operations:
            for p in op.parameters:
                collect_from_type(p.python_type)
            if op.body is not None:
                collect_from_type(op.body.type_name or "")
            for r in op.responses:
                if r.type_name:
                    collect_from_type(r.type_name)

        for name in list(self.models):
            if name not in used:
                del self.models[name]


def fetch_openapi(base_url: str, *, timeout: float = 5.0) -> dict:
    """从运行中的服务获取 OpenAPI JSON。

    任何网络层失败统一抛 :class:`ServiceUnreachableError`；
    端点可达但内容不是合法 JSON 时抛 :class:`ContractFetchError`。
    """
    from .errors import ContractFetchError

    base = base_url.rstrip("/")
    url = f"{base}{_OPENAPI_PATH}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
            charset = resp.headers.get_content_charset() or "utf-8"
            data = resp.read().decode(charset)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ServiceUnreachableError(
                f"服务 {base} 未暴露 {_OPENAPI_PATH} (HTTP 404)"
            ) from exc
        raise ServiceUnreachableError(
            f"获取契约失败: HTTP {exc.code} ({url})"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ServiceUnreachableError(f"无法连接服务 {base}: {exc}") from exc

    if status >= 400:
        raise ServiceUnreachableError(f"获取契约失败: HTTP {status} ({url})")
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ContractFetchError(
            f"{url} 返回的内容不是合法 JSON: {exc.msg} (第 {exc.lineno} 行)"
        ) from exc
    if not isinstance(parsed, dict):
        raise ContractFetchError(f"{url} 返回的 JSON 根节点不是对象")
    return parsed


def parse_contract(spec: dict, *, include_tags: set[str] | None = None) -> ContractIR:
    """把 OpenAPI 文档解析并归约为 :class:`ContractIR`。

    :param include_tags: 仅纳入这些 tag 下的操作；None 表示纳入全部操作。
    """
    return _Parser(spec, include_tags=include_tags).parse()
