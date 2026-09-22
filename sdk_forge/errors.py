"""Distinguishable error categories used across the forge pipeline."""


class ForgeError(Exception):
    """Base class for all forge errors; carries a stable machine-readable code."""

    code = "forge_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class ServiceUnreachableError(ForgeError):
    """Raised when the contract endpoint cannot be contacted."""

    code = "service_unreachable"


class ContractFetchError(ForgeError):
    """Raised when the contract endpoint responds but not with a usable contract."""

    code = "contract_fetch_failed"


class MalformedContractError(ForgeError):
    """Raised when a contract fragment is missing or cannot be parsed.

    ``path`` identifies the JSON pointer / location of the offending fragment so
    that callers can distinguish *which* fragment failed rather than silently
    skipping it.
    """

    code = "malformed_contract"

    def __init__(self, message: str, *, path: str = "$", details: dict | None = None):
        merged = {"path": path, **(details or {})}
        super().__init__(message, details=merged)
        self.path = path


class VersionMismatchError(ForgeError):
    """Raised when a requested contract version does not match the served one."""

    code = "version_mismatch"


class IncompatibleContractError(ForgeError):
    """Raised (or reported) when a breaking change is detected."""

    code = "incompatible_contract"

    def __init__(self, message: str, *, changes: list | None = None):
        super().__init__(message, details={"breaking_changes": len(changes or [])})
        self.changes = changes or []


class GenerationInProgressError(ForgeError):
    """Raised when an identical generation request is already running."""

    code = "generation_in_progress"


class ArtifactWriteError(ForgeError):
    """Raised when generated artifacts cannot be materialized atomically."""

    code = "artifact_write_failed"
