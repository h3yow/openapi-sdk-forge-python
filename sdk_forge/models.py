"""契约中间表示（IR）与兼容性判定结果的数据结构。

解析器把 OpenAPI 文档归约为这些结构，代码生成器与兼容性检查器
都只依赖 IR，不直接触碰原始 OpenAPI JSON。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Parameter:
    """单个参数（path / query / header）。"""

    name: str
    location: str  # "path" | "query" | "header"
    required: bool
    python_type: str  # 渲染后的 Python 类型表达式
    var_name: str  # 合法 Python 标识符
    schema_raw: dict = field(default_factory=dict)


@dataclass
class BodyInfo:
    """请求体。"""

    required: bool
    type_name: str | None  # models.py 中的模型类名；无 schema 时为 None
    content_type: str
    var_name: str = "body"
    schema_raw: dict = field(default_factory=dict)


@dataclass
class ResponseInfo:
    """单个响应状态。"""

    status: str  # "200" / "404" / "default"
    type_name: str | None  # 响应模型类名；无内容时为 None
    description: str = ""
    schema_raw: dict = field(default_factory=dict)


@dataclass
class ModelField:
    """数据模型字段。"""

    name: str
    python_type: str
    required: bool
    description: str = ""
    schema_raw: dict = field(default_factory=dict)


@dataclass
class SchemaModel:
    """具名对象模型（来自 components.schemas 或内联提取）。"""

    name: str
    fields: list[ModelField] = field(default_factory=list)
    description: str = ""
    is_enum: bool = False
    enum_values: list[str] = field(default_factory=list)
    raw_schema: dict = field(default_factory=dict)


@dataclass
class Operation:
    """一个接口操作（method + path）。"""

    operation_id: str
    method: str
    path: str
    tag: str
    parameters: list[Parameter] = field(default_factory=list)
    body: BodyInfo | None = None
    responses: list[ResponseInfo] = field(default_factory=list)
    python_name: str = ""

    @property
    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"


@dataclass
class ContractIR:
    """归约后的契约。"""

    title: str
    version: str | None
    operations: list[Operation] = field(default_factory=list)
    models: dict[str, SchemaModel] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class BreakingChange:
    """一条破坏性变更。

    :param location: 精确定位，例如 "GET /items/{id} -> response 200 -> 字段 'name'"。
    :param kind: 变更类别，例如 "field_removed" / "type_narrowed" /
        "required_added" / "enum_narrowed" / "endpoint_removed" /
        "parameter_removed" / "response_removed"。
    :param detail: 人类可读的判定依据。
    """

    location: str
    kind: str
    detail: str

    def format(self) -> str:
        return f"[{self.kind}] {self.location}: {self.detail}"


@dataclass
class CompatibilityReport:
    """一次兼容性判定的完整结论（可序列化、可复现）。"""

    old_version: str | None
    new_version: str | None
    compatible: bool
    rules_version: str
    breaking_changes: list[BreakingChange] = field(default_factory=list)
    accepted_changes: list[str] = field(default_factory=list)
    checked_at: str = ""

    def to_dict(self) -> dict:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "compatible": self.compatible,
            "rules_version": self.rules_version,
            "breaking_changes": [
                {"location": c.location, "kind": c.kind, "detail": c.detail}
                for c in self.breaking_changes
            ],
            "accepted_changes": self.accepted_changes,
            "checked_at": self.checked_at,
        }
