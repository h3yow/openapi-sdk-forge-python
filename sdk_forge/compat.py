"""Rule-versioned backward-compatibility checking between contracts.

Rule set versioning
-------------------
Every released rule set is frozen in :data:`RULE_SETS` and addressed by an id
(``RULE_VERSION`` is the current default).  Each emitted :class:`Change`
carries the individual rule id that produced it, and every stored report
records the rule-set version.  Pinning ``rule_version`` re-runs the exact
frozen logic that originally produced a verdict, so historical verdicts stay
reproducible after the rules themselves evolve.

Evolution example kept on purpose:
    * ``rules-2026.09`` ships ``R105-enum-changed``: *any* enum value-set
      difference is breaking (strict).
    * ``rules-2026.12`` replaces it with ``R205-enum-narrowed``: only removal
      of enum values (narrowing) is breaking; adding values is accepted.

Compatibility rules (current default)
-------------------------------------
Accepted (non-breaking):
    * adding new operations;
    * adding optional request fields / optional query parameters;
    * adding response fields, strengthening response guarantees
      (optional -> required, nullable -> non-nullable);
    * type widening (integer -> number) in either direction;
    * adding enum values;
    * adding additional documented response status codes.

Rejected (breaking, with operation + field/path attribution):
    * removing an operation;
    * removing any field or parameter;
    * making a request parameter/field mandatory ("必填化");
    * weakening a response guarantee (required -> optional,
      non-nullable -> nullable);
    * type narrowing (number -> integer) and any other unsupported type move;
    * removing enum values;
    * removing a documented response status code.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass

from .contract import Contract, Operation, Schema

logger = logging.getLogger("sdk_forge.compat")

RULE_VERSION_V1 = "rules-2026.09"
RULE_VERSION = "rules-2026.12"

_TYPE_WIDENING = {("integer", "number")}


class Severity(str, enum.Enum):
    ACCEPT = "accept"
    BREAKING = "breaking"
    INFO = "info"


@dataclass(frozen=True)
class Change:
    rule_id: str
    severity: Severity
    location: str  # e.g. "operation:getItem / response:200 / field:price"
    message: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "location": self.location,
            "message": self.message,
        }


@dataclass(frozen=True)
class CompatibilityReport:
    compatible: bool
    rule_version: str
    old_fingerprint: str
    new_fingerprint: str
    changes: list[Change]

    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.severity == Severity.BREAKING]

    def to_dict(self) -> dict:
        return {
            "compatible": self.compatible,
            "rule_version": self.rule_version,
            "old_fingerprint": self.old_fingerprint,
            "new_fingerprint": self.new_fingerprint,
            "changes": [c.to_dict() for c in self.changes],
        }


# --------------------------------------------------------------------------- #
# Schema traversal
# --------------------------------------------------------------------------- #

def _iter_fields(schema: Schema | None, prefix: str = ""):
    """Yield ``(dotted-path, Schema, is_required_by_parent)`` for each field.

    ``is_required`` reflects whether the *parent* object lists the field in its
    ``required`` array (``Schema.required`` itself is that parent-side list).
    """
    if schema is None:
        return
    if schema.is_object:
        for name, prop in schema.properties.items():
            path = f"{prefix}.{name}" if prefix else name
            yield path, prop, name in schema.required
            yield from _iter_fields(prop, path)
    elif schema.is_array and schema.items is not None:
        yield from _iter_fields(schema.items, f"{prefix}[]")


def _field_at(schema: Schema | None, path: str) -> tuple[Schema, bool] | None:
    """Return ``(field-schema, required-by-parent)`` at a dotted path."""
    if schema is None:
        return None
    current = schema
    required = False
    for segment in path.split("."):
        if segment.endswith("[]"):
            key = segment[:-2]
            if not (current.is_object and key in current.properties):
                return None
            current = current.properties[key]
            if not (current.is_array and current.items is not None):
                return None
            current = current.items
            required = True
            continue
        if current.is_object and segment in current.properties:
            required = segment in current.required
            current = current.properties[segment]
        else:
            return None
    return current, required


def _success_schema(op: Operation) -> Schema | None:
    """Schema of the 2xx response body, preferring the lowest 2xx code."""
    candidates = [r for r in op.responses if r.status_code.startswith("2")]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.status_code)
    return candidates[0].schema


# --------------------------------------------------------------------------- #
# Individual checks. Each returns nothing; it records onto ``changes``.
# Direction: "request" (client -> server) or "response" (server -> client).
# --------------------------------------------------------------------------- #

def _required_paths(schema: Schema | None, prefix: str = "") -> set[str]:
    """Paths of fields marked required by their immediate parent object."""
    if schema is None:
        return set()
    paths: set[str] = set()
    if schema.is_object:
        for name, prop in schema.properties.items():
            path = f"{prefix}.{name}" if prefix else name
            if name in schema.required:
                paths.add(path)
            paths |= _required_paths(prop, path)
    elif schema.is_array and schema.items is not None:
        paths |= _required_paths(schema.items, f"{prefix}[]")
    return paths


def _check_required_and_nullable(
    old: Schema, new: Schema, direction: str, location: str, changes: list[Change]
) -> None:
    old_required = _required_paths(old)
    new_required = _required_paths(new)
    for path, old_f, _ in _iter_fields(old):
        found = _field_at(new, path)
        if found is None:
            continue  # field removal handled by R002
        new_f, _ = found
        loc = f"{location} / field:{path}"
        if (path in old_required) != (path in new_required):
            if path not in old_required and path in new_required:
                if direction == "request":
                    changes.append(Change(
                        "R003-required-tightened", Severity.BREAKING, loc,
                        f"request field {path!r} became required (必填化)",
                    ))
                else:
                    changes.append(Change(
                        "R003-required-strengthened", Severity.ACCEPT, loc,
                        f"response field {path!r} is now always present",
                    ))
            elif direction == "response":
                changes.append(Change(
                    "R003-required-weakened", Severity.BREAKING, loc,
                    f"response field {path!r} may now be absent (guarantee weakened)",
                ))
            else:
                changes.append(Change(
                    "R003-required-relaxed", Severity.ACCEPT, loc,
                    f"request field {path!r} is no longer required",
                ))
        if old_f.nullable != new_f.nullable:
            if not old_f.nullable and new_f.nullable:
                if direction == "request":
                    changes.append(Change(
                        "R007-nullable-widened", Severity.ACCEPT, loc,
                        f"request field {path!r} now accepts null (type widened)",
                    ))
                else:
                    changes.append(Change(
                        "R007-nullable-weakened", Severity.BREAKING, loc,
                        f"response field {path!r} may now be null (guarantee weakened)",
                    ))
            elif direction == "request":
                changes.append(Change(
                    "R007-nullable-narrowed", Severity.BREAKING, loc,
                    f"request field {path!r} no longer allows null (type narrowed)",
                ))
            else:
                changes.append(Change(
                    "R007-nullable-strengthened", Severity.ACCEPT, loc,
                    f"response field {path!r} is guaranteed non-null",
                ))


def _field_schema(schema: Schema | None, path: str) -> Schema | None:
    found = _field_at(schema, path)
    return found[0] if found else None


def _check_types(
    old: Schema, new: Schema, location: str, changes: list[Change]
) -> None:
    for path, old_f, _ in _iter_fields(old):
        found = _field_at(new, path)
        if found is None:
            continue
        new_f, _ = found
        loc = f"{location} / field:{path}"
        if old_f.type != new_f.type:
            if (old_f.type, new_f.type) in _TYPE_WIDENING:
                changes.append(Change(
                    "R004-type-widened", Severity.ACCEPT, loc,
                    f"type widened {old_f.type!r} -> {new_f.type!r}",
                ))
            else:
                changes.append(Change(
                    "R004-type-narrowed", Severity.BREAKING, loc,
                    f"unsupported/type-narrowing change {old_f.type!r} -> {new_f.type!r}",
                ))
            continue
        if old_f.is_array and new_f.is_array:
            if old_f.items is not None and new_f.items is not None:
                item_loc = f"{location} / field:{path}[]"
                if old_f.items.type != new_f.items.type:
                    if (old_f.items.type, new_f.items.type) in _TYPE_WIDENING:
                        changes.append(Change("R004-type-widened", Severity.ACCEPT, item_loc,
                                             f"array item type widened {old_f.items.type!r} -> {new_f.items.type!r}"))
                    else:
                        changes.append(Change("R004-type-narrowed", Severity.BREAKING, item_loc,
                                             f"array item type narrowed/changed {old_f.items.type!r} -> {new_f.items.type!r}"))


def _check_fields_added_removed(
    old: Schema, new: Schema, direction: str, location: str, changes: list[Change]
) -> None:
    old_paths = {p for p, _, _ in _iter_fields(old)}
    new_paths = {p for p, _, _ in _iter_fields(new)}
    for removed in sorted(old_paths - new_paths):
        changes.append(Change(
            "R002-field-removed", Severity.BREAKING, f"{location} / field:{removed}",
            f"{direction} field {removed!r} was removed",
        ))
    for added in sorted(new_paths - old_paths):
        found = _field_at(new, added)
        now_required = bool(found and found[1])
        if direction == "request" and now_required:
            changes.append(Change(
                "R002-required-field-added", Severity.BREAKING,
                f"{location} / field:{added}",
                f"new required {direction} field {added!r} breaks existing clients",
            ))
        else:
            changes.append(Change(
                "R002-optional-field-added", Severity.ACCEPT,
                f"{location} / field:{added}",
                f"optional {direction} field {added!r} was added",
            ))


def _check_enums_strict(
    old: Schema, new: Schema, direction: str, location: str, changes: list[Change]
) -> None:
    """v1 policy: every enum value-set difference is breaking."""
    for path, old_f, _ in _iter_fields(old):
        new_f = _field_schema(new, path)
        if new_f is None or old_f.enum is None:
            continue
        if new_f.enum is not None and set(old_f.enum) != set(new_f.enum):
            changes.append(Change(
                "R105-enum-changed", Severity.BREAKING, f"{location} / field:{path}",
                f"{direction} enum value set changed: {sorted(map(str, old_f.enum))} -> "
                f"{sorted(map(str, new_f.enum))}",
            ))


def _check_enums_directional(
    old: Schema, new: Schema, direction: str, location: str, changes: list[Change]
) -> None:
    """v2 policy: removing values is narrowing (breaking); adding is accepted."""
    for path, old_f, _ in _iter_fields(old):
        new_f = _field_schema(new, path)
        if new_f is None or old_f.enum is None:
            continue
        if new_f.enum is None:
            changes.append(Change(
                "R205-enum-narrowed", Severity.BREAKING, f"{location} / field:{path}",
                f"{direction} enum constraint on {path!r} was removed",
            ))
            continue
        removed = set(map(str, old_f.enum)) - set(map(str, new_f.enum))
        added = set(map(str, new_f.enum)) - set(map(str, old_f.enum))
        if removed:
            changes.append(Change(
                "R205-enum-narrowed", Severity.BREAKING, f"{location} / field:{path}",
                f"{direction} enum values removed (narrowing): {sorted(removed)}",
            ))
        if added:
            changes.append(Change(
                "R205-enum-widened", Severity.ACCEPT, f"{location} / field:{path}",
                f"{direction} enum values added (widening): {sorted(added)}",
            ))


def _check_operations(old: Contract, new: Contract, changes: list[Change]) -> None:
    old_ops = old.operation_map()
    new_ops = new.operation_map()
    for op_id in sorted(old_ops.keys() - new_ops.keys()):
        changes.append(Change(
            "R001-operation-removed", Severity.BREAKING, f"operation:{op_id}",
            f"operation {op_id!r} ({old_ops[op_id].method} {old_ops[op_id].path}) was removed",
        ))
    for op_id in sorted(new_ops.keys() - old_ops.keys()):
        changes.append(Change(
            "R001-operation-added", Severity.ACCEPT, f"operation:{op_id}",
            f"operation {op_id!r} was added",
        ))


def _check_parameters(old_op: Operation, new_op: Operation, changes: list[Change]) -> None:
    loc = f"operation:{old_op.operation_id}"

    def index(op: Operation) -> dict[tuple[str, str], object]:
        return {(p.location, p.name): p for p in op.parameters}

    old_p, new_p = index(old_op), index(new_op)
    for key in sorted(old_p.keys() - new_p.keys()):
        changes.append(Change(
            "R006-parameter-removed", Severity.BREAKING,
            f"{loc} / parameter:{key[0]}:{key[1]}",
            f"{key[0]} parameter {key[1]!r} was removed",
        ))
    for key in sorted(new_p.keys() - old_p.keys()):
        param = new_p[key]
        severity = Severity.BREAKING if param.required else Severity.ACCEPT
        rule = "R006-required-parameter-added" if param.required else "R006-optional-parameter-added"
        changes.append(Change(
            rule, severity, f"{loc} / parameter:{key[0]}:{key[1]}",
            f"{'required' if param.required else 'optional'} {key[0]} parameter {key[1]!r} was added",
        ))
    for key in sorted(old_p.keys() & new_p.keys()):
        before, after = old_p[key], new_p[key]
        if not before.required and after.required:
            changes.append(Change(
                "R006-parameter-made-required", Severity.BREAKING,
                f"{loc} / parameter:{key[0]}:{key[1]}",
                f"{key[0]} parameter {key[1]!r} became required (必填化)",
            ))
        elif before.required and not after.required:
            changes.append(Change(
                "R006-parameter-made-optional", Severity.ACCEPT,
                f"{loc} / parameter:{key[0]}:{key[1]}",
                f"{key[0]} parameter {key[1]!r} is no longer required",
            ))
        if before.schema.type != after.schema.type:
            if (before.schema.type, after.schema.type) in _TYPE_WIDENING:
                changes.append(Change(
                    "R004-type-widened", Severity.ACCEPT,
                    f"{loc} / parameter:{key[0]}:{key[1]}",
                    f"parameter type widened {before.schema.type!r} -> {after.schema.type!r}",
                ))
            else:
                changes.append(Change(
                    "R004-type-narrowed", Severity.BREAKING,
                    f"{loc} / parameter:{key[0]}:{key[1]}",
                    f"parameter type narrowed/changed {before.schema.type!r} -> {after.schema.type!r}",
                ))


def _check_status_codes(old_op: Operation, new_op: Operation, changes: list[Change]) -> None:
    loc = f"operation:{old_op.operation_id}"
    old_codes = {r.status_code for r in old_op.responses}
    new_codes = {r.status_code for r in new_op.responses}
    for code in sorted(old_codes - new_codes):
        changes.append(Change(
            "R008-response-status-removed", Severity.BREAKING,
            f"{loc} / response:{code}",
            f"documented response status {code!r} was removed",
        ))
    for code in sorted(new_codes - old_codes):
        changes.append(Change(
            "R008-response-status-added", Severity.ACCEPT,
            f"{loc} / response:{code}",
            f"response status {code!r} was added",
        ))


# --------------------------------------------------------------------------- #
# Frozen rule sets
# --------------------------------------------------------------------------- #

def _body_checks(direction: str, enum_check: Callable) -> Callable:
    def check(old_op: Operation, new_op: Operation, changes: list[Change]) -> None:
        if direction == "request":
            old_body, new_body = old_op.request_body, new_op.request_body
            location = f"operation:{old_op.operation_id} / request"
        else:
            old_body, new_body = _success_schema(old_op), _success_schema(new_op)
            location = f"operation:{old_op.operation_id} / response:success"
        if old_body is None and new_body is None:
            return
        if old_body is None:
            changes.append(Change(
                "R002-optional-field-added", Severity.ACCEPT,
                f"operation:{old_op.operation_id} / {direction}",
                f"{direction} body was introduced",
            ))
            return
        if new_body is None:
            changes.append(Change(
                "R002-field-removed", Severity.BREAKING,
                f"operation:{old_op.operation_id} / {direction}",
                f"{direction} body was removed",
            ))
            return
        _check_fields_added_removed(old_body, new_body, direction, location, changes)
        _check_required_and_nullable(old_body, new_body, direction, location, changes)
        _check_types(old_body, new_body, location, changes)
        enum_check(old_body, new_body, direction, location, changes)

    return check


def _compose(enum_check: Callable) -> Callable:
    request_check = _body_checks("request", enum_check)
    response_check = _body_checks("response", enum_check)

    def run(old: Contract, new: Contract, changes: list[Change]) -> None:
        _check_operations(old, new, changes)
        for op_id in sorted(old.operation_map().keys() & new.operation_map().keys()):
            old_op, new_op = old.operation_map()[op_id], new.operation_map()[op_id]
            _check_parameters(old_op, new_op, changes)
            _check_status_codes(old_op, new_op, changes)
            request_check(old_op, new_op, changes)
            response_check(old_op, new_op, changes)

    return run


RULE_SETS: dict[str, tuple[str, Callable]] = {
    RULE_VERSION_V1: (
        "frozen initial rules: any enum change is breaking",
        _compose(_check_enums_strict),
    ),
    RULE_VERSION: (
        "directional enum rules: value removal breaks, value addition accepted",
        _compose(_check_enums_directional),
    ),
}


def check_backward_compatible(
    old: Contract, new: Contract, *, rule_version: str = RULE_VERSION
) -> CompatibilityReport:
    """Compare two contracts and return a reproducible compatibility report."""
    if rule_version not in RULE_SETS:
        raise ValueError(
            f"unknown rule_version {rule_version!r}; known: {sorted(RULE_SETS)}"
        )
    logger.info(
        "compat input rule_version=%s old=(version=%s fp=%s ops=%s) new=(version=%s fp=%s ops=%s)",
        rule_version, old.version, old.fingerprint[:12],
        sorted(op.operation_id for op in old.operations),
        new.version, new.fingerprint[:12],
        sorted(op.operation_id for op in new.operations),
    )
    changes: list[Change] = []
    _description, run = RULE_SETS[rule_version]
    run(old, new, changes)
    changes.sort(key=lambda c: (c.severity.value, c.rule_id, c.location))
    compatible = not any(c.severity == Severity.BREAKING for c in changes)

    report = CompatibilityReport(
        compatible=compatible,
        rule_version=rule_version,
        old_fingerprint=old.fingerprint,
        new_fingerprint=new.fingerprint,
        changes=changes,
    )
    for change in changes:
        logger.info(
            "compat basis rule_id=%s severity=%s location=%s verdict_detail=%s",
            change.rule_id, change.severity.value, change.location, change.message,
        )
    logger.info(
        "compat verdict compatible=%s rule_version=%s breaking=%d accepted=%d",
        compatible, rule_version, len(report.breaking()),
        len(changes) - len(report.breaking()),
    )
    return report
