"""根据 :class:`ContractIR` 生成客户端 SDK 代码。

产物是一个零第三方依赖（仅标准库）的 Python 包，包含：

* ``models.py``：请求/响应数据模型（dataclass）与枚举（Enum）
* ``client.py``：按 tag 分组的调用方法，覆盖路径参数、查询参数、
  请求体与全部已声明响应状态
* ``__init__.py`` / ``py.typed`` / ``metadata.json``
"""

from __future__ import annotations

import io
import json
import keyword
import zipfile
from collections import OrderedDict

from .models import ContractIR, Operation, SchemaModel

_DATETIME_TYPES = {"datetime"}


def _safe(name: str) -> str:
    if keyword.iskeyword(name):
        return name + "_"
    return name


def _render_field_type(t: str) -> tuple[str, bool]:
    """返回 (注解字符串, 是否需要运行时字符串注解)。

    前向引用（包内模型名）统一以字符串注解给出，避免类定义顺序问题。
    """
    # 把裸模型名 / 联合/泛型里的模型名加引号
    tokens = t.replace("|", " | ").replace("[", "[ ").replace("]", " ]").split()
    out: list[str] = []
    needs_future = False
    builtins = {
        "str", "int", "float", "bool", "bytes", "object",
        "list", "dict", "None", "datetime",
    }
    for tok in tokens:
        bare = tok.strip("[],")
        if bare and bare[0].isupper() and bare not in builtins:
            out.append(tok.replace(bare, f'"{bare}"'))
            needs_future = True
        elif bare == "datetime":
            needs_future = True
            out.append(tok)
        else:
            out.append(tok)
    return " ".join(out).replace("[ ", "[").replace(" ]", "]"), needs_future


class _Renderer:
    def __init__(self, ir: ContractIR, package_name: str) -> None:
        self.ir = ir
        self.package = package_name

    # -- models.py -----------------------------------------------------------

    def render_models(self) -> str:
        lines: list[str] = [
            '"""由 OpenAPI 契约自动生成的请求/响应模型，请勿手改。"""',
            "",
            "from __future__ import annotations",
            "",
            "from dataclasses import dataclass, field",
            "from datetime import datetime",
            "from enum import Enum",
            "from typing import Any",
            "",
            "",
        ]
        for name, model in self.ir.models.items():
            if model.is_enum:
                lines.extend(self._render_enum(name, model))
            else:
                lines.extend(self._render_object(name, model))
            lines.append("")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_enum(self, name: str, model: SchemaModel) -> list[str]:
        out = [f"class {name}(str, Enum):"]
        if model.description:
            out.insert(0, f'"""{model.description}"""')
        used: set[str] = set()
        for value in model.enum_values:
            member = value.upper().replace("-", "_").replace("/", "_")
            if not member or member[0].isdigit() or keyword.iskeyword(member):
                member = f"VALUE_{member}"
            base, i = member, 2
            while member in used:
                member = f"{base}_{i}"
                i += 1
            used.add(member)
            out.append(f'    {member} = {json.dumps(value, ensure_ascii=False)}')
        return out

    def _render_object(self, name: str, model: SchemaModel) -> list[str]:
        out = ["@dataclass", f"class {name}:"]
        if model.description:
            out.append(f'    """{model.description}"""')
        if not model.fields:
            out.append("    pass")
            return out
        # 必填字段在前，可选在后（dataclass 排序约束）
        required = [f for f in model.fields if f.required]
        optional = [f for f in model.fields if not f.required]
        for f in required:
            out.extend(self._render_field(f))
        for f in optional:
            out.extend(self._render_field(f))
        return out

    def _render_field(self, f) -> list[str]:
        annotation, _ = _render_field_type(f.python_type)
        lines = []
        if f.description:
            lines.append(f"    # {f.description}")
        if f.required:
            lines.append(f"    {_safe(f.name)}: {annotation}")
        else:
            # 可选字段给 None 默认值；带裸模型名的联合本身包含 None
            lines.append(f"    {_safe(f.name)}: {annotation} = None")
        return lines

    # -- client.py -----------------------------------------------------------

    def render_client(self) -> str:
        lines = [
            '"""由 OpenAPI 契约自动生成的 HTTP 客户端，仅依赖标准库。"""',
            "",
            "from __future__ import annotations",
            "",
            "import json as _json",
            "import urllib.error",
            "import urllib.parse",
            "import urllib.request",
            "from dataclasses import dataclass",
            "from typing import Any",
            "",
            "from . import models",
            "",
            "",
            "class ApiError(Exception):",
            '    """服务端返回 4xx/5xx。"""',
            "",
            "    def __init__(self, status_code: int, body: Any):",
            "        self.status_code = status_code",
            "        self.body = body",
            "        super().__init__(f'HTTP {status_code}: {body!r}')",
            "",
            "",
            "@dataclass",
            "class _Response:",
            "    status_code: int",
            "    data: Any",
            "",
            "",
            "class ApiClient:",
            '    """同步 HTTP 客户端。"""',
            "",
            "    def __init__(self, base_url: str, *, timeout: float = 10.0,",
            "                 headers: dict[str, str] | None = None):",
            "        self.base_url = base_url.rstrip('/')",
            "        self.timeout = timeout",
            "        self.headers = dict(headers or {})",
            "",
            "    def _request(self, method: str, path: str, *,",
            "                 path_params: dict[str, Any] | None = None,",
            "                 query_params: dict[str, Any] | None = None,",
            "                 body: Any = None,",
            "                 content_type: str = 'application/json',",
            "                 headers: dict[str, str] | None = None):",
            "        for key, value in (path_params or {}).items():",
            "            path = path.replace('{' + key + '}',",
            "                              urllib.parse.quote(str(value), safe=''))",
            "        url = self.base_url + path",
            "        query = {k: v for k, v in (query_params or {}).items()",
            "                 if v is not None}",
            "        if query:",
            "            url += '?' + urllib.parse.urlencode(query, doseq=True)",
            "        data = None",
            "        req_headers = dict(self.headers)",
            "        req_headers.update(headers or {})",
            "        if body is not None:",
            "            data = _json.dumps(self._plain(body)).encode('utf-8')",
            "            req_headers['Content-Type'] = content_type",
            "        req = urllib.request.Request(url, data=data,",
            "                                     headers=req_headers, method=method)",
            "        try:",
            "            with urllib.request.urlopen(req, timeout=self.timeout) as resp:",
            "                text = resp.read().decode('utf-8')",
            "                status = resp.status",
            "        except urllib.error.HTTPError as exc:",
            "            text = exc.read().decode('utf-8', errors='replace')",
            "            raise ApiError(exc.code, self._maybe_json(text)) from exc",
            "        except urllib.error.URLError as exc:",
            "            raise ConnectionError(f'请求失败: {exc}') from exc",
            "        return _Response(status, self._maybe_json(text))",
            "",
            "    @staticmethod",
            "    def _maybe_json(text: str):",
            "        if not text:",
            "            return None",
            "        try:",
            "            return _json.loads(text)",
            "        except ValueError:",
            "            return text",
            "",
            "    @staticmethod",
            "    def _plain(value: Any) -> Any:",
            "        if hasattr(value, '__dict__'):",
            "            return {k: ApiClient._plain(v) for k, v in value.__dict__.items()",
            "                    if v is not None}",
            "        if isinstance(value, list):",
            "            return [ApiClient._plain(v) for v in value]",
            "        if isinstance(value, dict):",
            "            return {k: ApiClient._plain(v) for k, v in value.items()}",
            "        return value",
            "",
        ]

        # 按 tag 分组
        groups: "OrderedDict[str, list[Operation]]" = OrderedDict()
        for op in self.ir.operations:
            groups.setdefault(op.tag, []).append(op)
        if not groups:
            groups = OrderedDict(default=[])
        for tag, ops in groups.items():
            if tag == "default" and not ops:
                continue
            mixin = self._mixin_name(tag)
            lines.append(f"class {mixin}:")
            for op in ops:
                lines.extend(self._render_method(op))
                lines.append("")
            lines.append("")
            lines.append("")

        bases = ", ".join(self._mixin_name(t) for t in groups)
        lines.append(f"class Client({bases}, ApiClient):")
        lines.append('    """组合全部接口分组的客户端。"""')
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _mixin_name(tag: str) -> str:
        clean = "".join(p.capitalize() for p in tag.replace("-", "_").split("_") if p)
        if not clean:
            clean = "Default"
        return f"_{clean}Api"

    def _render_method(self, op: Operation) -> list[str]:
        # 参数顺序：path -> 必填 query -> body -> 可选 query
        path_params = [p for p in op.parameters if p.location == "path"]
        query_params = [p for p in op.parameters if p.location == "query"]
        header_params = [p for p in op.parameters if p.location == "header"]
        req_q = [p for p in query_params if p.required]
        opt_q = [p for p in query_params if not p.required]

        args = ["self"]
        for p in path_params + req_q:
            ann, _ = _render_field_type(p.python_type)
            args.append(f"{_safe(p.var_name)}: {ann}")
        if op.body is not None:
            body_ann, _ = _render_field_type(op.body.type_name or "Any")
            if op.body.required:
                args.append(f"body: {body_ann}")
        for p in opt_q:
            ann, _ = _render_field_type(p.python_type)
            args.append(f"{_safe(p.var_name)}: {ann} = None")
        if op.body is not None and not op.body.required:
            body_ann, _ = _render_field_type(op.body.type_name or "Any")
            args.append(f"body: {body_ann} = None")
        for p in header_params:
            ann, _ = _render_field_type(p.python_type)
            if p.required:
                args.append(f"{_safe(p.var_name)}: {ann}")
            else:
                args.append(f"{_safe(p.var_name)}: {ann} = None")

        out = [f"    def {_safe(op.python_name)}({', '.join(args)}) -> _Response:"]
        out.append(f'        """{op.method} {op.path}"""')
        if path_params:
            out.append("        path_params = {")
            for p in path_params:
                out.append(f"            {p.name!r}: {_safe(p.var_name)},")
            out.append("        }")
        else:
            out.append("        path_params = None")
        all_q = query_params
        if all_q:
            out.append("        query_params = {")
            for p in all_q:
                out.append(f"            {p.name!r}: {_safe(p.var_name)},")
            out.append("        }")
        else:
            out.append("        query_params = None")
        kwargs = [
            "path_params=path_params",
            "query_params=query_params",
        ]
        if op.body is not None:
            kwargs.append("body=body")
            kwargs.append(f"content_type={op.body.content_type!r}")
        if header_params:
            out.append("        headers = {")
            for p in header_params:
                out.append(f"            {p.name!r}: {_safe(p.var_name)},")
            out.append("        }")
            # _request 暂不直接支持自定义 header；合并到实例 headers 副本
            kwargs.append("headers=headers")
        out.append(
            f"        return self._request({op.method!r}, {op.path!r}, "
            + ", ".join(kwargs) + ")"
        )
        return out

    # -- 包文件 --------------------------------------------------------------

    def render_init(self) -> str:
        model_names = ", ".join(sorted(self.ir.models))
        return (
            '"""自动生成的 SDK 包。"""\n\n'
            "from .client import ApiError, ApiClient, Client\n"
            "from . import models\n\n"
            f"__all__ = ['ApiError', 'ApiClient', 'Client', 'models']\n"
            f"MODELS = [{', '.join(repr(n) for n in sorted(self.ir.models))}]\n"
        )

    def render_metadata(self) -> dict:
        return {
            "title": self.ir.title,
            "version": self.ir.version,
            "operations": [
                {
                    "name": op.python_name,
                    "method": op.method,
                    "path": op.path,
                }
                for op in self.ir.operations
            ],
            "models": sorted(self.ir.models),
        }


def generate_sdk(ir: ContractIR, *, package_name: str = "generated_sdk") -> dict[str, str]:
    """返回 ``{相对路径: 文件内容}`` 的 SDK 产物映射。"""
    renderer = _Renderer(ir, package_name)
    files = {
        f"{package_name}/__init__.py": renderer.render_init(),
        f"{package_name}/models.py": renderer.render_models(),
        f"{package_name}/client.py": renderer.render_client(),
        f"{package_name}/py.typed": "",
        f"{package_name}/metadata.json": json.dumps(
            renderer.render_metadata(), ensure_ascii=False, indent=2
        ),
    }
    return files


def render_sdk_zip(files: dict[str, str]) -> bytes:
    """把产物映射打包为内存中的 zip。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buffer.getvalue()
