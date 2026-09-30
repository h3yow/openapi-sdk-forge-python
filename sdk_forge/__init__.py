"""openapi-sdk-forge: 契约驱动的 SDK 生成与兼容性校验。"""

from .errors import (
    ForgeError,
    GenerationInProgressError,
    IncompatibleContractError,
    JudgementInProgressError,
    MalformedContractError,
    MissingContractFragmentError,
    ServiceUnreachableError,
    UnknownRulesVersionError,
    VersionMismatchError,
)
from .models import (
    AcceptedChange,
    BreakingChange,
    CompatibilityReport,
    ContractIR,
)

__all__ = [
    "ForgeError",
    "ServiceUnreachableError",
    "MalformedContractError",
    "MissingContractFragmentError",
    "VersionMismatchError",
    "IncompatibleContractError",
    "GenerationInProgressError",
    "JudgementInProgressError",
    "UnknownRulesVersionError",
    "ContractIR",
    "CompatibilityReport",
    "BreakingChange",
    "AcceptedChange",
]
