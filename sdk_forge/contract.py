"""OpenAPI contract loading, parsing and normalization.

Supported scope (enforced by the parser; unsupported/missing fragments raise
:class:`MalformedContractError` instead of being skipped):

* OpenAPI 3.0.x / 3.1.x documents served as JSON.
* ``paths`` operations with ``parameters`` (path + query), a JSON
  ``requestBody`` and one or more JSON responses keyed by status code.
* Schema objects supporting ``$ref``, ``type`` (object/array/string/integer/
  number/boolean), ``properties``, ``required``, ``items``, ``enum``,
  ``nullable`` (3.0) / ``type: ["x", "null"]`` (3.1) and ``format``
  (retained but not enforced).
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .errors import (
    ContractFetchError,
    MalformedContractError,
    ServiceUnreachableError,
    VersionMismatchError,
)

SUPPORTED_OPENAPI_PREFIX = "3."
_ALLOWED_TYPES = {"object", "array", "string", "integer", "number", "boolean"}
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_MAX_SCHEMA_DEPTH = 64


@dataclass(frozen=True)
class Schema:
    """A normalized JSON-schema fragment."""

    type: str
    name: str | None = None
    properties: dict[str, "Schema"] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    items: "Schema | None" = None
    enum: list | None = None
    nullable: bool = False
    format: str | None = None
    ref: str | None = None

    @property
    def is_object(self) -> bool:
        return self.type == "object"

    @property
    def is_array(self) -> bool:
        return self.type == "array"


@dataclass(frozen=True)
class Parameter:
    name: str
    location: str  # "path" or "query"
    required: bool
    schema: Schema


@dataclass(frozen=True)
class Response:
    status_code: str  # e.g. "200", "404", "default"
    schema: Schema | None


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    parameters: list[Parameter]
    request_body: Schema | None
    request_required: bool
    responses: list[Response]


@dataclass(frozen=True)
class Contract:
    """A fully parsed contract plus provenance metadata."""

    title: str
    version: str
    raw: dict
    operations: list[Operation]
    schemas: dict[str, Schema]
    fingerprint: str
    openapi: str

    def operation_map(self) -> dict[str, Operation]:
        return {op.operation_id: op for op in self.operations}


def _fingerprint(raw: dict) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fetch_contract_raw(base_url: str, *, timeout: float = 10.0) -> tuple[dict, str]:
    """Fetch the raw OpenAPI document from a running service.

    The document is fetched from ``<base_url>/openapi.json`` (the FastAPI
    default).  Network failures map to :class:`ServiceUnreachableError`;
    bad HTTP status / non-JSON bodies map to :class:`ContractFetchError`.
    """
    url = urljoin(base_url.rstrip("/") + "/", "openapi.json")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise ContractFetchError(
            f"contract endpoint returned HTTP {exc.code}",
            details={"url": url, "status": exc.code},
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ServiceUnreachableError(
            f"cannot reach contract endpoint {url}: {exc.reason if isinstance(exc, urllib.error.URLError) else exc}",
            details={"url": url, "reason": str(getattr(exc, "reason", exc))},
        ) from exc

    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractFetchError(
            f"contract endpoint did not return valid JSON: {exc}",
            details={"url": url},
        ) from exc
    if not isinstance(document, dict):
        raise ContractFetchError(
            "contract document is not a JSON object", details={"url": url}
        )
    return document, url


class _SchemaResolver:
    def __init__(self, raw: dict):
        self._raw = raw
        self._named: dict[str, Schema] = {}
        self._resolving: set[str] = set()

    def named_schemas(self) -> dict[str, Schema]:
        return dict(self._named)

    def _resolve_ref(self, ref: str, depth: int) -> Schema:
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            raise MalformedContractError(
                f"unsupported $ref {ref!r}; only {prefix}<Name> references are supported",
                path=ref,
            )
        name = ref[len(prefix):]
        if name in self._named:
            return self._named[name]
        if name in self._resolving:
            raise MalformedContractError(
                f"cyclic schema reference detected at {ref!r}",
                path=ref,
                details={"resolving": sorted(self._resolving)},
            )
        components = self._raw.get("components")
        schemas = components.get("schemas") if isinstance(components, dict) else None
        if not isinstance(schemas, dict) or name not in schemas:
            raise MalformedContractError(
                f"$ref {ref!r} points at a schema that does not exist", path=ref
            )
        self._resolving.add(name)
        try:
            schema = self.resolve(schemas[name], ref, depth=depth, name=name)
        finally:
            self._resolving.discard(name)
        self._named[name] = schema
        return schema

    def resolve(self, node: object, path: str, *, depth: int = 0, name: str | None = None) -> Schema:
        if not isinstance(node, dict):
            raise MalformedContractError("schema must be a JSON object", path=path)
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref:
                raise MalformedContractError("$ref must be a non-empty string", path=path)
            return self._resolve_ref(ref, depth)
        node = self._normalize_anyof(node, path)
        return self._build(node, depth, path, name=name)

    def _normalize_anyof(self, node: dict, path: str) -> dict:
        """Rewrite OpenAPI 3.1 / Pydantic v2 nullable unions.

        ``{"anyOf": [{"type": T}, {"type": "null"}]}`` -> ``{"type": T,
        "nullable": True}``.  Any other ``anyOf`` composition is unsupported
        and reported rather than guessed.
        """
        if "anyOf" not in node:
            return node
        alternatives = node["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise MalformedContractError("'anyOf' must be a non-empty list", path=path)
        non_null = [a for a in alternatives if a.get("type") != "null"]
        has_null = any(a.get("type") == "null" for a in alternatives)
        if not has_null or len(non_null) != 1 or not isinstance(non_null[0], dict):
            raise MalformedContractError(
                f"unsupported 'anyOf' composition {alternatives!r}; only "
                "'<type> | null' unions are supported",
                path=f"{path}/anyOf",
            )
        merged = {k: v for k, v in node.items() if k != "anyOf"}
        merged.update(non_null[0])
        merged["nullable"] = True
        return merged

    def _build(self, node: dict, depth: int, path: str, *, name: str | None) -> Schema:
        if depth > _MAX_SCHEMA_DEPTH:
            raise MalformedContractError(
                f"schema nesting exceeds maximum depth {_MAX_SCHEMA_DEPTH}", path=path
            )
        if "$ref" in node:
            return self.resolve(node, path, depth=depth)

        raw_type = node.get("type")
        nullable = bool(node.get("nullable", False))
        # OpenAPI 3.1: type: ["string", "null"]
        if isinstance(raw_type, list):
            non_null = [t for t in raw_type if t != "null"]
            nullable = nullable or ("null" in raw_type)
            if len(non_null) != 1:
                raise MalformedContractError(
                    f"unsupported type array {raw_type!r}; exactly one non-null type required",
                    path=path,
                )
            raw_type = non_null[0]
        if not isinstance(raw_type, str) or not raw_type:
            raise MalformedContractError(
                "schema is missing a supported scalar 'type'", path=path
            )
        if raw_type not in _ALLOWED_TYPES:
            raise MalformedContractError(
                f"unsupported schema type {raw_type!r}; allowed: {sorted(_ALLOWED_TYPES)}",
                path=path,
            )

        enum = node.get("enum")
        if enum is not None and not isinstance(enum, list):
            raise MalformedContractError("'enum' must be a list", path=path)

        fmt = node.get("format")
        if fmt is not None and not isinstance(fmt, str):
            raise MalformedContractError("'format' must be a string", path=path)

        properties: dict[str, Schema] = {}
        required_raw = node.get("required", [])
        if not isinstance(required_raw, list) or not all(isinstance(r, str) for r in required_raw):
            raise MalformedContractError("'required' must be a list of strings", path=path)

        if raw_type == "object":
            props = node.get("properties")
            if props is None:
                raise MalformedContractError(
                    "object schema is missing 'properties'", path=path
                )
            if not isinstance(props, dict):
                raise MalformedContractError("'properties' must be an object", path=path)
            for prop_name, prop_node in props.items():
                properties[prop_name] = self.resolve(
                    prop_node, f"{path}/properties/{prop_name}", depth=depth + 1
                )
            missing = [r for r in required_raw if r not in properties]
            if missing:
                raise MalformedContractError(
                    f"required properties not declared in properties: {missing}",
                    path=f"{path}/required",
                )
        items: Schema | None = None
        if raw_type == "array":
            if "items" not in node:
                raise MalformedContractError("array schema is missing 'items'", path=path)
            items = self.resolve(node["items"], f"{path}/items", depth=depth + 1)

        return Schema(
            type=raw_type,
            name=name,
            properties=properties,
            required=list(required_raw),
            items=items,
            enum=list(enum) if enum is not None else None,
            nullable=nullable,
            format=fmt,
            ref=None,
        )


def _parse_parameter(
    raw_param: object, resolver: _SchemaResolver, path: str
) -> Parameter:
    if not isinstance(raw_param, dict):
        raise MalformedContractError("parameter must be an object", path=path)
    name = raw_param.get("name")
    location = raw_param.get("in")
    if not isinstance(name, str) or not name:
        raise MalformedContractError("parameter is missing a 'name'", path=path)
    if location not in ("path", "query"):
        raise MalformedContractError(
            f"unsupported parameter location {location!r}; only path/query are supported",
            path=f"{path}/in",
        )
    required = bool(raw_param.get("required", location == "path"))
    if location == "path" and not raw_param.get("required", True):
        raise MalformedContractError(
            f"path parameter {name!r} must be required", path=f"{path}/required"
        )
    if "schema" not in raw_param:
        raise MalformedContractError(
            f"parameter {name!r} is missing a schema", path=f"{path}/schema"
        )
    schema = resolver.resolve(raw_param["schema"], f"{path}/schema")
    return Parameter(name=name, location=location, required=required, schema=schema)


def _json_schema_of_content(content: object, path: str) -> Schema | bool:
    """Return (schema-or-None, required). Raises on unsupported media types."""
    if not isinstance(content, dict):
        raise MalformedContractError("'content' must be a media-type object", path=path)
    if "application/json" not in content:
        raise MalformedContractError(
            f"only application/json payloads are supported, found: {sorted(content)}",
            path=path,
        )
    media = content["application/json"]
    if not isinstance(media, dict) or "schema" not in media:
        raise MalformedContractError(
            "application/json media type is missing a schema", path=path
        )
    return media  # type: ignore[return-value]


def parse_contract(raw: dict, *, source: str = "<inline>") -> Contract:
    """Parse and validate a raw OpenAPI document into a :class:`Contract`."""
    if not isinstance(raw, dict):
        raise MalformedContractError("contract root must be a JSON object", path="$")

    openapi = raw.get("openapi")
    if not isinstance(openapi, str) or not openapi.startswith(SUPPORTED_OPENAPI_PREFIX):
        raise MalformedContractError(
            f"only OpenAPI {SUPPORTED_OPENAPI_PREFIX}x documents are supported, got {openapi!r}",
            path="/openapi",
        )

    info = raw.get("info")
    if not isinstance(info, dict):
        raise MalformedContractError("contract is missing an 'info' object", path="/info")
    version = info.get("version")
    if not isinstance(version, str) or not version:
        raise MalformedContractError("info.version is required", path="/info/version")
    title = info.get("title")
    if not isinstance(title, str) or not title:
        raise MalformedContractError("info.title is required", path="/info/title")

    paths = raw.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise MalformedContractError("contract must declare at least one path", path="/paths")

    resolver = _SchemaResolver(raw)
    operations: list[Operation] = []
    seen_ids: set[str] = set()

    for url_path, item in paths.items():
        if not isinstance(url_path, str) or not url_path.startswith("/"):
            raise MalformedContractError("path keys must start with '/'", path=f"/paths/{url_path}")
        if not isinstance(item, dict):
            raise MalformedContractError("path item must be an object", path=f"/paths{url_path}")
        if "parameters" in item:
            raise MalformedContractError(
                "path-item level 'parameters' are not supported; declare parameters per operation",
                path=f"/paths{url_path}/parameters",
            )
        for method, op_node in item.items():
            if method not in _HTTP_METHODS:
                continue
            op_path = f"/paths{url_path}/{method}"
            if not isinstance(op_node, dict):
                raise MalformedContractError("operation must be an object", path=op_path)
            op_id = op_node.get("operationId")
            if not isinstance(op_id, str) or not op_id:
                raise MalformedContractError(
                    "operation is missing a non-empty 'operationId'", path=f"{op_path}/operationId"
                )
            if op_id in seen_ids:
                raise MalformedContractError(
                    f"duplicate operationId {op_id!r}", path=f"{op_path}/operationId"
                )
            seen_ids.add(op_id)

            parameters: list[Parameter] = []
            for i, param_node in enumerate(op_node.get("parameters", [])):
                parameters.append(
                    _parse_parameter(param_node, resolver, f"{op_path}/parameters/{i}")
                )

            request_body: Schema | None = None
            request_required = False
            if "requestBody" in op_node:
                rb = op_node["requestBody"]
                rb_path = f"{op_path}/requestBody"
                if not isinstance(rb, dict):
                    raise MalformedContractError("requestBody must be an object", path=rb_path)
                request_required = bool(rb.get("required", False))
                media = _json_schema_of_content(rb.get("content"), f"{rb_path}/content")
                request_body = resolver.resolve(media["schema"], f"{rb_path}/content/application/json/schema")

            responses_node = op_node.get("responses")
            if not isinstance(responses_node, dict) or not responses_node:
                raise MalformedContractError(
                    f"operation {op_id!r} must declare at least one response",
                    path=f"{op_path}/responses",
                )
            responses: list[Response] = []
            for status, resp_node in responses_node.items():
                if not isinstance(status, str):
                    raise MalformedContractError("response status must be a string key", path=op_path)
                resp_path = f"{op_path}/responses/{status}"
                if not isinstance(resp_node, dict):
                    raise MalformedContractError("response must be an object", path=resp_path)
                schema: Schema | None = None
                if "content" in resp_node:
                    media = _json_schema_of_content(resp_node["content"], f"{resp_path}/content")
                    schema = resolver.resolve(
                        media["schema"], f"{resp_path}/content/application/json/schema"
                    )
                responses.append(Response(status_code=status, schema=schema))

            operations.append(
                Operation(
                    operation_id=op_id,
                    method=method.upper(),
                    path=url_path,
                    parameters=parameters,
                    request_body=request_body,
                    request_required=request_required,
                    responses=responses,
                )
            )

    # Force resolution of every component schema so malformed fragments there
    # are reported even when no operation references them.
    components = raw.get("components")
    if isinstance(components, dict) and isinstance(components.get("schemas"), dict):
        for name, node in components["schemas"].items():
            resolver.resolve(node, f"/components/schemas/{name}", name=name)

    return Contract(
        title=title,
        version=version,
        raw=raw,
        operations=operations,
        schemas=resolver.named_schemas(),
        fingerprint=_fingerprint(raw),
        openapi=openapi,
    )


def load_contract(
    base_url: str, *, expected_version: str | None = None, timeout: float = 10.0
) -> Contract:
    """Fetch and parse a contract, optionally asserting its version."""
    raw, _url = fetch_contract_raw(base_url, timeout=timeout)
    contract = parse_contract(raw, source=base_url)
    if expected_version is not None and contract.version != expected_version:
        raise VersionMismatchError(
            f"contract version mismatch: requested {expected_version!r}, service serves {contract.version!r}",
            details={
                "expected_version": expected_version,
                "served_version": contract.version,
                "fingerprint": contract.fingerprint,
            },
        )
    return contract
