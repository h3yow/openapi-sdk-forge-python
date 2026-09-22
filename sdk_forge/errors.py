"""SDK Forge 统一错误类型。

每类错误对应一种可区分的失败原因，API 层据此映射不同的 HTTP 状态码，
测试也据此对错误分类做断言。
"""

from __future__ import annotations


class ForgeError(Exception):
    """所有 forge 错误的基类。"""

    code: str = "forge_error"
    http_status: int = 500


class ServiceUnreachableError(ForgeError):
    """服务不可达（连接被拒绝 / 超时 / 非 2xx 响应）。"""

    code = "service_unreachable"
    http_status = 502


class ContractFetchError(ForgeError):
    """契约端点可达，但返回的内容无法解析为 JSON / OpenAPI 文档。"""

    code = "contract_fetch_error"
    http_status = 502


class MalformedContractError(ForgeError):
    """契约可解析为 JSON，但结构不符合受支持的 OpenAPI 子集。"""

    code = "malformed_contract"
    http_status = 422


class MissingContractFragmentError(ForgeError):
    """契约引用了缺失或无法解析的片段（如悬空 $ref）。

    :param fragment: 无法解析的片段标识，例如 "#/components/schemas/Foo"。
    """

    code = "missing_contract_fragment"
    http_status = 422

    def __init__(self, fragment: str, *, context: str = "") -> None:
        self.fragment = fragment
        self.context = context
        detail = f"缺失或无法解析的契约片段: {fragment}"
        if context:
            detail += f" (位于 {context})"
        super().__init__(detail)


class VersionMismatchError(ForgeError):
    """请求的契约版本与运行时实际版本不一致。"""

    code = "version_mismatch"
    http_status = 409

    def __init__(self, expected: str | None, actual: str | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"契约版本不匹配: 请求 {expected!r}, 运行时 {actual!r}")


class IncompatibleContractError(ForgeError):
    """新旧契约之间存在破坏性变更。

    :param breaking_changes: 破坏性变更描述列表。
    """

    code = "incompatible_contract"
    http_status = 409

    def __init__(self, breaking_changes: list[str]) -> None:
        self.breaking_changes = breaking_changes
        super().__init__(
            "检测到破坏性契约变更:\n  - " + "\n  - ".join(breaking_changes)
        )


class GenerationInProgressError(ForgeError):
    """同一契约版本已有生成任务正在执行（并发重复触发）。"""

    code = "generation_in_progress"
    http_status = 409

    def __init__(self, contract_version: str) -> None:
        self.contract_version = contract_version
        super().__init__(f"契约版本 {contract_version} 的生成任务正在进行中")
