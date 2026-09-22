"""Client SDK code generation from a parsed contract.

The generated package is plain Python and only relies on the standard library
(``urllib``), so generated artifacts stay runnable without extra dependencies.

Generated layout::

    <package>/
        __init__.py     - re-exports Client, ApiError and all models
        models.py       - one dataclass per object schema + enum constants
        client.py       - one method per operation: path/query params, JSON
                          body, a parsed success model and ApiError on errors
        codegen.json    - provenance (contract title/version/fingerprint)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .contract import Contract, Operation, Schema

_PRIMITIVE_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
}


@dataclass(frozen=True)
class GeneratedFile:
    path: str  # relative path inside the artifact directory
    content: str


@dataclass(frozen=True)
class GeneratedBundle:
    files: list[GeneratedFile]
    package_name: str
    contract_title: str
    contract_version: str
    fingerprint: str

    def file_map(self) -> dict[str, str]:
        return {gen_file.path: gen_file.content for gen_file in self.files}


def _identifier(name: str) -> str:
    ident = re.sub(r"[^0-9a-zA-Z_]", "_", str(name))
    if not ident or ident[0].isdigit():
        ident = f"_{ident}"
    return ident


def _snake(name: str) -> str:
    ident = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    ident = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", ident).lower()
    ident = re.sub(r"_+", "_", ident).strip("_")
    if not ident:
        ident = "model"
    if ident[0].isdigit():
        ident = f"_{ident}"
    return ident


def _pascal(name: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", str(name))
    out = "".join(p[:1].upper() + p[1:] for p in parts if p) or "Model"
    if out[0].isdigit():
        out = f"_{out}"
    return out


def _path_resource_tokens(path: str) -> list[str]:
    """Meaningful tokens of a URL path, dropping parameter placeholders.

    ``/items/{item_id}`` -> ["items"]; ``/orders/{id}/lines`` -> ["orders", "lines"].
    """
    tokens: list[str] = []
    for segment in path.strip("/").split("/"):
        if not segment or segment.startswith("{"):
            continue
        tokens.append(_snake(segment))
    return tokens


def _method_name(op: Operation) -> str:
    """Derive a readable client method name from method + path.

    Examples:
        GET  /items/{id}        -> get_item
        POST /items             -> create_item
        GET  /                  -> <snake(operationId)>
        PUT  /items/{id}/tags   -> update_item_tags
    Explicit operationIds on custom paths are used as a fallback.
    """
    resources = _path_resource_tokens(op.path)
    verb_map = {
        "GET": "get", "POST": "create", "PUT": "replace",
        "PATCH": "update", "DELETE": "delete",
    }
    verb = verb_map.get(op.method, _snake(op.method))
    if not resources:
        return _snake(op.operation_id)
    singular = resources[-1]
    # naive singularisation for common plural resources
    if singular.endswith("ies"):
        singular = singular[:-3] + "y"
    elif singular.endswith("s") and not singular.endswith("ss"):
        singular = singular[:-1]
    if op.method == "GET":
        has_path_param = any(p.location == "path" for p in op.parameters)
        name = f"get_{singular}" if has_path_param else f"list_{resources[-1]}"
    else:
        name = f"{verb}_{singular}"
    return name


class _ModelRegistry:
    """Collects component-named and inline object schemas into unique classes."""

    def __init__(self, contract: Contract):
        self.models: dict[str, Schema] = {}
        self._by_id: dict[int, str] = {}
        for name, schema in contract.schemas.items():
            self.models[name] = schema
            self._by_id[id(schema)] = name

    def register_inline(self, schema: Schema | None, suggested: str) -> str | None:
        if schema is None:
            return None
        if id(schema) in self._by_id:
            return self._by_id[id(schema)]
        name = _pascal(suggested)
        base, i = name, 2
        while name in self.models:
            name = f"{base}{i}"
            i += 1
        self.models[name] = schema
        self._by_id[id(schema)] = name
        self._register_children(schema, name)
        return name

    def _register_children(self, schema: Schema, parent_name: str) -> None:
        if schema.is_array and schema.items is not None:
            self.register_inline(schema.items, f"{parent_name}Item")
        elif schema.is_object:
            for prop_name, prop in schema.properties.items():
                self.register_inline(prop, f"{parent_name}_{prop_name}")

    def model_name(self, schema: Schema | None) -> str | None:
        if schema is None:
            return None
        return self._by_id.get(id(schema))

    def py_type(self, schema: Schema | None) -> str:
        if schema is None:
            return "object"
        named = self.model_name(schema)
        if named:
            return named
        if schema.is_array:
            return f"list[{self.py_type(schema.items)}]"
        if schema.is_object:
            return "dict"
        base = _PRIMITIVE_TYPES.get(schema.type, "object")
        return f"{base} | None" if schema.nullable else base


def _parse_expr(registry: _ModelRegistry, schema: Schema | None, var: str) -> str | None:
    """Return an expression converting raw value ``var`` into typed Python."""
    if schema is None:
        return None
    named = registry.model_name(schema)
    if named:
        return f"{named}.from_dict({var})"
    if schema.is_array and schema.items is not None:
        item = registry.model_name(schema.items)
        if item:
            return f"[{item}.from_dict(_i) for _i in {var}]"
        inner_expr = _parse_expr(registry, schema.items, "_i")
        if inner_expr and inner_expr != "_i":
            return f"[{inner_expr} for _i in {var}]"
        return f"list({var})"
    if schema.is_object:
        return None  # plain dict passthrough
    return None  # primitive passthrough


def _render_model(
    name: str, schema: Schema, registry: _ModelRegistry
) -> tuple[list[str], list[tuple[str, list]]]:
    lines: list[str] = ["@dataclass", f"class {name}:"]
    enums: list[tuple[str, list]] = []
    if not schema.properties:
        lines += ["    pass", ""]
        return lines, enums

    for prop_name, prop in schema.properties.items():
        attr = _identifier(prop_name)
        py_type = registry.py_type(prop)
        if prop.enum is not None:
            enum_name = f"{name}_{_pascal(prop_name).upper()}"
            enum_name = f"{name}{_pascal(prop_name)}"
            enums.append((enum_name, list(prop.enum)))
            py_type = "str"
        if prop_name not in schema.required:
            lines.append(f"    {attr}: {py_type} | None = None")
        else:
            lines.append(f"    {attr}: {py_type}")
    lines.append("")

    lines += [
        "    @classmethod",
        f"    def from_dict(cls, data: dict) -> '{name}':",
        "        if not isinstance(data, dict):",
        "            return cls()",
        "        kwargs = {}",
    ]
    for prop_name, prop in schema.properties.items():
        key = prop_name
        attr = _identifier(prop_name)
        lines.append(f"        if {key!r} in data and data[{key!r}] is not None:")
        expr = _parse_expr(registry, prop, f"data[{key!r}]")
        lines.append(f"            kwargs[{attr!r}] = {expr or f'data[{key!r}]'}")
    lines.append("        return cls(**kwargs)")
    lines.append("")
    return lines, enums


def _render_models(contract: Contract, registry: _ModelRegistry) -> str:
    out = [
        '"""Request/response models generated from the service contract."""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
    ]
    all_enums: list[tuple[str, list]] = []
    for name in sorted(registry.models):
        model_lines, enums = _render_model(name, registry.models[name], registry)
        out.extend(model_lines)
        all_enums.extend(enums)

    if all_enums:
        out += ["# Enum value sets declared by the contract (documented, not",
                "# enforced client-side; the server remains the authority).", ""]
        for enum_name, values in all_enums:
            const = enum_name.upper()
            rendered = ", ".join(json.dumps(v) for v in values)
            out.append(f"{const} = ({rendered},)")
        out.append("")
    return "\n".join(out)


def _method_names(contract: Contract) -> dict[str, str]:
    """Map operationId -> unique snake-case Python method name."""
    names: dict[str, str] = {}
    used: set[str] = set()
    for op in sorted(contract.operations, key=lambda o: o.operation_id):
        base = _method_name(op)
        candidate, i = base, 2
        while candidate in used:
            candidate = f"{base}_{i}"
            i += 1
        used.add(candidate)
        names[op.operation_id] = candidate
    return names


def _render_method(op: Operation, registry: _ModelRegistry, method_name: str) -> list[str]:
    path_params = [p for p in op.parameters if p.location == "path"]
    query_params = [p for p in op.parameters if p.location == "query"]

    args = ["self"]
    for p in path_params:
        args.append(f"{_identifier(p.name)}: {registry.py_type(p.schema)}")
    for p in query_params:
        args.append(f"{_identifier(p.name)}: {registry.py_type(p.schema)} | None = None")
    if op.request_body is not None:
        body_type = registry.py_type(op.request_body)
        if op.request_required:
            args.append(f"body: {body_type}")
        else:
            args.append(f"body: {body_type} | None = None")

    lines = [
        "",
        f"    def {method_name}({', '.join(args)}):",
        f'        """{op.method} {op.path} (operationId={op.operation_id})"""',
    ]

    fmt_path = op.path
    for p in path_params:
        fmt_path = fmt_path.replace("{" + p.name + "}", "{" + _identifier(p.name) + "}")
    lines.append(f"        path = f'{fmt_path}'")

    if query_params:
        pairs = ", ".join(
            f"{_identifier(p.name)!r}: {_identifier(p.name)}" for p in query_params
        )
        lines.append(f"        query = {{{pairs}}}")
    else:
        lines.append("        query = None")

    if op.request_body is not None:
        if registry.model_name(op.request_body):
            lines += [
                "        _raw_body = dataclasses.asdict(body) if body is not None else None",
                "        # omit unset optional fields so 'null' is not sent for them",
                "        payload = {k: v for k, v in _raw_body.items() if v is not None} if _raw_body is not None else None",
            ]
        else:
            lines.append("        payload = body")
    else:
        lines.append("        payload = None")

    success = sorted(
        (r for r in op.responses if r.status_code.startswith("2")),
        key=lambda r: r.status_code,
    )
    expect = "None"
    if success and success[0].schema is not None:
        s = success[0].schema
        named = registry.model_name(s)
        if named:
            expect = f"_models.{named}.from_dict"
        elif s.is_array:
            item = registry.model_name(s.items) if s.items else None
            if item:
                expect = f"lambda data: [_models.{item}.from_dict(i) for i in (data or [])]"
    lines.append(
        f"        return self._request({op.method!r}, path, query=query, body=payload, expect={expect})"
    )
    lines.append("")
    return lines


def _render_client(contract: Contract, registry: _ModelRegistry) -> str:
    method_names = _method_names(contract)
    lines = [
        '"""HTTP client generated from the service contract (stdlib only)."""',
        "",
        "from __future__ import annotations",
        "",
        "import dataclasses",
        "import json",
        "import urllib.error",
        "import urllib.parse",
        "import urllib.request",
        "",
        "from . import models as _models",
        "",
        "",
        "class ApiError(Exception):",
        '    """Raised for non-success HTTP responses returned by the service."""',
        "",
        "    def __init__(self, status_code: int, body):",
        "        self.status_code = status_code",
        "        self.body = body",
        '        super().__init__(f"API request failed with status {status_code}")',
        "",
        "",
        "class Client:",
        "    def __init__(self, base_url: str, *, timeout: float = 30.0, token: str | None = None):",
        "        self.base_url = base_url.rstrip('/')",
        "        self.timeout = timeout",
        "        self._token = token",
        "",
        "    def _request(self, method, path, *, query=None, body=None, expect=None):",
        "        if query:",
        "            filtered = {k: v for k, v in query.items() if v is not None}",
        "            if filtered:",
        "                path += '?' + urllib.parse.urlencode(filtered, doseq=True)",
        "        data = None",
        "        headers = {'Accept': 'application/json'}",
        "        if body is not None:",
        "            data = json.dumps(body).encode('utf-8')",
        "            headers['Content-Type'] = 'application/json'",
        "        if self._token:",
        "            headers['Authorization'] = f'Bearer {self._token}'",
        "        req = urllib.request.Request(",
        "            self.base_url + path, data=data, headers=headers, method=method",
        "        )",
        "        try:",
        "            with urllib.request.urlopen(req, timeout=self.timeout) as resp:",
        "                raw, status = resp.read(), resp.status",
        "        except urllib.error.HTTPError as exc:",
        "            raw, status = exc.read(), exc.code",
        "        parsed = json.loads(raw.decode('utf-8')) if raw else None",
        "        if not 200 <= status < 300:",
        "            raise ApiError(status, parsed)",
        "        return expect(parsed) if expect else parsed",
    ]
    for op in contract.operations:
        lines.extend(_render_method(op, registry, method_names[op.operation_id]))
    return "\n".join(lines)


def generate_sdk(contract: Contract) -> GeneratedBundle:
    """Render a complete, importable client SDK bundle for ``contract``."""
    registry = _ModelRegistry(contract)
    for op in contract.operations:
        for param in op.parameters:
            registry.register_inline(
                param.schema, f"{op.operation_id}_{param.location}_{param.name}_param"
            )
        registry.register_inline(op.request_body, f"{op.operation_id}_request")
        for resp in op.responses:
            registry.register_inline(
                resp.schema, f"{op.operation_id}_response_{_pascal(resp.status_code)}"
            )

    models_py = _render_models(contract, registry)
    client_py = _render_client(contract, registry)

    model_names = sorted(registry.models)
    init_py = (
        '"""Generated SDK. Do not edit; regenerate from the service contract."""\n\n'
        "from .client import ApiError, Client\n"
        f"from .models import {', '.join(model_names)}\n\n"
        "__all__ = ['ApiError', 'Client', "
        + ", ".join(repr(n) for n in model_names)
        + "]\n"
    )
    codegen = json.dumps(
        {
            "contract_title": contract.title,
            "contract_version": contract.version,
            "contract_fingerprint": contract.fingerprint,
            "openapi": contract.openapi,
            "operations": [op.operation_id for op in contract.operations],
            "models": model_names,
        },
        indent=2,
        sort_keys=True,
    )

    package_name = f"{_snake(contract.title)}_sdk"
    return GeneratedBundle(
        files=[
            GeneratedFile("__init__.py", init_py),
            GeneratedFile("models.py", models_py),
            GeneratedFile("client.py", client_py),
            GeneratedFile("codegen.json", codegen + "\n"),
        ],
        package_name=package_name,
        contract_title=contract.title,
        contract_version=contract.version,
        fingerprint=contract.fingerprint,
    )
