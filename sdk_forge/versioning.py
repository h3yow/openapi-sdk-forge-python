"""契约版本提取与向后兼容性判定（请求/响应方向分离 + 规则集冻结）。

判定规则由规则版本号标识；规则演进时发布新版本号，**旧版本完整保留**，
历史判定可凭归档契约与 rules_version 逐字节复现（见 :func:`check_with_rules`、
:func:`compare_across_rules`）。

兼容性总原则：旧客户端 + 新服务。请求方向与响应方向语义不同，分开判定：

请求方向（客户端 -> 服务端）：

* 新增必填参数/请求体字段、删除参数/字段、可选 -> 必填、类型收窄、枚举收窄：拒绝
* 新增可选参数/字段、必填 -> 可选、类型放宽、枚举新增取值：接受
* 「新增必填字段判破坏」的覆盖深度随规则版本冻结：2026.09.01 只覆盖请求体
  **顶层**对象（嵌套对象/参数对象内的必填新增当时按放宽接受）；
  2026.10.01 起覆盖请求方向任意嵌套深度。旧版本语义不得就地修改。

响应方向（服务端 -> 客户端，2026.10.01 起与请求方向分离）：

* 字段 **必填 -> 可选**：服务端可能不再返回该字段，旧客户端会读到缺失数据，
  自 2026.10.01 起判为破坏（``required_removed``）；在旧规则 2026.09.01 下放宽接受
* 字段 **可选 -> 必填**：服务端给出更强保证，接受
* 删除响应字段/响应状态、类型收窄、枚举收窄、响应体 schema 删除：拒绝
* 新增响应字段（必填与否）、新增响应状态、类型放宽、枚举新增取值：接受

每条结论都带接口（``location``）、方向（``direction``）、类别（``kind``）与依据。
"""

from __future__ import annotations

import datetime
from typing import Any

from .errors import UnknownRulesVersionError
from .models import (
    AcceptedChange,
    BreakingChange,
    CompatibilityReport,
    ContractIR,
    DIRECTION_ENDPOINT,
    DIRECTION_REQUEST,
    DIRECTION_RESPONSE,
)

# 旧规则集（冻结）：响应方向「必填 -> 可选」当时按放宽处理；
# 请求方向「新增必填字段判破坏」当时只覆盖请求体顶层对象。
RULES_VERSION_2026_09 = "2026.09.01"
# 当前规则集：响应方向「必填 -> 可选」判为破坏；
# 请求方向「新增必填字段判破坏」覆盖任意嵌套深度。
RULES_VERSION = "2026.10.01"

# 类型放宽关系：old -> 允许的 new 集合（consumer 收到更宽的值安全）
_WIDENINGS: dict[str, set[str]] = {
    "integer": {"number"},
}

# 被认为等价/可互换的 format 放宽（旧 format -> 新允许的 format 集合，None=无格式）
_FORMAT_WIDENINGS: dict[str | None, set[str | None]] = {
    "date-time": {None},
    "date": {"date-time", None},
}


def extract_version(spec_or_ir: dict | ContractIR) -> str | None:
    """提取契约版本（info.version）。"""
    if isinstance(spec_or_ir, ContractIR):
        return spec_or_ir.version
    info = spec_or_ir.get("info")
    if isinstance(info, dict):
        v = info.get("version")
        return v if isinstance(v, str) else None
    return None


def available_rules_versions() -> list[str]:
    """所有已冻结规则版本（旧 -> 新）。"""
    return sorted(_RULESETS)


# ---------------------------------------------------------------------------
# Schema 归一化与比较
# ---------------------------------------------------------------------------

def _deref(schema: Any, spec: dict) -> dict:
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        node: Any = spec
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict) and node:
            return node
    return schema if isinstance(schema, dict) else {}


def _normalize(schema: dict, spec: dict) -> dict:
    """解 ref + 把 anyOf/oneOf [T, null] 折叠为 nullable T。"""
    schema = _deref(schema, spec)
    for combiner in ("anyOf", "oneOf"):
        if combiner in schema and isinstance(schema[combiner], list):
            members = schema[combiner]
            non_null = [m for m in members if _deref(m, spec).get("type") != "null"]
            nullable = len(non_null) < len(members)
            if len(non_null) == 1:
                base = dict(_normalize(non_null[0], spec))
                base["x_nullable"] = nullable
                return base
            # 多成员 union：保留归一化后的成员列表
            return {
                "x_union": [_normalize(m, spec) for m in non_null],
                "x_nullable": nullable,
            }
    out = dict(schema)
    out.pop("anyOf", None)
    out.pop("oneOf", None)
    out.setdefault("x_nullable", False)
    return out


def _type_of(n: dict) -> str | None:
    if "x_union" in n:
        return "union"
    return n.get("type")


def _union_types(n: dict) -> list[str]:
    return [m.get("type") for m in n.get("x_union", [])]


def _acc(
    accepted: list[AcceptedChange],
    location: str,
    kind: str,
    detail: str,
    direction: str,
) -> None:
    accepted.append(AcceptedChange(location, kind, detail, direction))


class _RuleSet:
    """单个规则版本的全部判定逻辑。

    :param response_required_relaxed: 响应方向「必填 -> 可选」是否按放宽接受。
        2026.09.01 为 True（历史行为），2026.10.01 起为 False（判为破坏）。
    :param request_required_deep: 请求方向「新增必填字段判破坏」是否覆盖任意
        嵌套深度。2026.09.01 为 False（只覆盖请求体顶层对象，嵌套必填新增
        按放宽接受），2026.10.01 起为 True。
    """

    def __init__(
        self,
        version: str,
        *,
        response_required_relaxed: bool,
        request_required_deep: bool,
    ) -> None:
        self.version = version
        self.response_required_relaxed = response_required_relaxed
        self.request_required_deep = request_required_deep

    # -- schema 级 -----------------------------------------------------------

    def diff_schemas(
        self,
        old_raw: dict,
        new_raw: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[AcceptedChange],
        direction: str,
        *,
        body_root: bool = False,
    ) -> list[BreakingChange]:
        """比较两处 schema，返回破坏性变更。

        :param direction: ``DIRECTION_REQUEST`` / ``DIRECTION_RESPONSE``，
            必填性语义随方向变化；删字段、类型/枚举收窄两个方向一致拒绝。
        :param body_root: 是否为请求体顶层对象。旧规则（2026.09.01）的
            「新增必填字段判破坏」只覆盖这一层，嵌套层按放宽接受。
        """
        o = _normalize(old_raw, spec_old)
        n = _normalize(new_raw, spec_new)
        return self._diff_normalized(
            o, n, spec_old, spec_new, location, accepted, direction,
            body_root=body_root,
        )

    def _diff_normalized(
        self,
        o: dict,
        n: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[AcceptedChange],
        direction: str,
        *,
        body_root: bool = False,
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []

        ot, nt = _type_of(o), _type_of(n)

        # union
        if ot == "union" or nt == "union":
            changes.extend(
                self._diff_union(
                    o, n, spec_old, spec_new, location, accepted, direction
                )
            )
            return changes

        # 空 type：视为任意
        if ot is None and nt is not None:
            # 旧契约无约束 -> 新契约束窄为具体类型：破坏
            changes.append(BreakingChange(
                location, "type_narrowed",
                f"类型由无约束收窄为 {nt!r}", direction,
            ))
            return changes
        if ot is not None and nt is None:
            _acc(accepted, location, "type_widened",
                 f"类型由 {ot!r} 放宽为无约束", direction)
            return changes
        if ot is None and nt is None:
            return changes

        # nullability：可空 -> 非空 是收窄（两个方向都拒绝）
        if o.get("x_nullable") and not n.get("x_nullable"):
            changes.append(BreakingChange(
                location, "type_narrowed",
                "由可空(nullable)变为非空，旧客户端可能收到/发送 null", direction,
            ))
        elif not o.get("x_nullable") and n.get("x_nullable"):
            _acc(accepted, location, "nullable_added",
                 "变为可空(nullable)，属于放宽", direction)

        # 标量类型放宽 / 收窄
        if ot != nt:
            allowed = _WIDENINGS.get(ot, set())
            if nt in allowed:
                _acc(accepted, location, "type_widened",
                     f"类型由 {ot!r} 放宽为 {nt!r}", direction)
            else:
                changes.append(BreakingChange(
                    location, "type_narrowed",
                    f"类型由 {ot!r} 变更为 {nt!r}，不属于允许的放宽 "
                    f"({sorted(allowed) or '无'})",
                    direction,
                ))
            return changes

        # format 放宽
        of, nf = o.get("format"), n.get("format")
        if of != nf and nt == "string":
            allowed_fmts = _FORMAT_WIDENINGS.get(of, set())
            if nf not in allowed_fmts and nf != of:
                changes.append(BreakingChange(
                    location, "type_narrowed",
                    f"string format 由 {of!r} 变更为 {nf!r}，不属于允许的放宽",
                    direction,
                ))
            elif nf != of:
                _acc(accepted, location, "format_widened",
                     f"format 由 {of!r} 放宽为 {nf!r}", direction)

        # 枚举：新值必须是旧值超集（两个方向一致）
        oe = o.get("enum")
        ne = n.get("enum")
        if oe is not None or ne is not None:
            old_vals = list(oe or [])
            new_vals = list(ne or [])
            removed = [v for v in old_vals if v not in new_vals]
            added = [v for v in new_vals if v not in old_vals]
            if removed:
                changes.append(BreakingChange(
                    location, "enum_narrowed",
                    f"枚举删除了取值 {removed}（旧: {old_vals}, 新: {new_vals}）",
                    direction,
                ))
            if added and not removed:
                _acc(accepted, location, "enum_widened",
                     f"枚举新增取值 {added}", direction)

        # 对象字段（必填性语义随方向变化）
        if nt == "object" and ("properties" in o or "properties" in n):
            changes.extend(
                self._diff_object(
                    o, n, spec_old, spec_new, location, accepted, direction,
                    body_root=body_root,
                )
            )

        # 数组元素
        if nt == "array":
            oi, ni = o.get("items"), n.get("items")
            if oi is not None and ni is not None:
                changes.extend(
                    self.diff_schemas(
                        oi, ni, spec_old, spec_new,
                        f"{location}[]", accepted, direction,
                    )
                )

        # 字典附加属性
        if nt == "object" and "additionalProperties" in n and isinstance(
            n["additionalProperties"], dict
        ):
            old_ap = o.get("additionalProperties")
            if isinstance(old_ap, dict):
                changes.extend(
                    self.diff_schemas(
                        old_ap, n["additionalProperties"], spec_old, spec_new,
                        f"{location}.<value>", accepted, direction,
                    )
                )

        return changes

    def _diff_union(
        self,
        o: dict,
        n: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[AcceptedChange],
        direction: str,
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []
        if _type_of(o) != "union" or _type_of(n) != "union":
            # union <-> 具体类型：只允许 具体类型 -> 包含该类型的 union（放宽）
            if _type_of(o) != "union" and _type_of(n) == "union":
                if _type_of(o) in _union_types(n):
                    _acc(accepted, location, "union_widened",
                         "具体类型扩展为联合类型", direction)
                    return changes
            changes.append(BreakingChange(
                location, "type_narrowed",
                f"联合类型结构不兼容: {_type_of(o)!r} -> {_type_of(n)!r}",
                direction,
            ))
            return changes
        old_members = {m.get("type"): m for m in o.get("x_union", [])}
        new_members = {m.get("type"): m for m in n.get("x_union", [])}
        removed = set(old_members) - set(new_members)
        added = set(new_members) - set(old_members)
        if removed:
            changes.append(BreakingChange(
                location, "enum_narrowed",
                f"联合类型删除了成员 {sorted(removed)}", direction,
            ))
        if added:
            _acc(accepted, location, "enum_widened",
                 f"联合类型新增成员 {sorted(added)}", direction)
        return changes

    def _diff_object(
        self,
        o: dict,
        n: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[AcceptedChange],
        direction: str,
        *,
        body_root: bool = False,
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []
        old_props = o.get("properties", {})
        new_props = n.get("properties", {})
        old_required = set(o.get("required", []))
        new_required = set(n.get("required", []))

        for fname in old_props:
            floc = f"{location}.{fname}"
            if fname not in new_props:
                # 字段删除：请求/响应两个方向都拒绝
                changes.append(BreakingChange(
                    floc, "field_removed", f"字段 {fname!r} 被删除", direction,
                ))
                continue

            was_required = fname in old_required
            now_required = fname in new_required
            if was_required and not now_required:
                # 必填 -> 可选：方向语义相反
                if direction == DIRECTION_RESPONSE:
                    if self.response_required_relaxed:
                        _acc(accepted, floc, "required_relaxed",
                             "响应字段由必填变为可选（旧规则下按放宽接受）",
                             direction)
                    else:
                        changes.append(BreakingChange(
                            floc, "required_removed",
                            f"响应字段 {fname!r} 由必填变为可选，"
                            f"服务端可能不再返回该字段，旧客户端会读到缺失数据",
                            direction,
                        ))
                else:
                    _acc(accepted, floc, "required_relaxed",
                         "字段由必填变为可选，属于放宽", direction)
            elif not was_required and now_required:
                if direction == DIRECTION_REQUEST:
                    changes.append(BreakingChange(
                        floc, "required_added",
                        f"字段 {fname!r} 由可选变为必填", direction,
                    ))
                else:
                    _acc(accepted, floc, "required_strengthened",
                         "响应字段由可选变为必填，服务端给出更强保证", direction)

            changes.extend(
                self.diff_schemas(
                    old_props[fname], new_props[fname],
                    spec_old, spec_new, floc, accepted, direction,
                )
            )

        for fname in new_props:
            if fname not in old_props:
                floc = f"{location}.{fname}"
                if direction == DIRECTION_REQUEST and fname in new_required:
                    if self.request_required_deep or body_root:
                        # 请求方向新增必填字段：破坏
                        changes.append(BreakingChange(
                            floc, "required_added",
                            f"请求体新增必填字段 {fname!r}", direction,
                        ))
                    else:
                        # 2026.09.01 冻结语义：必填新增判破坏只覆盖请求体顶层，
                        # 嵌套层（嵌套对象/参数对象/数组元素）当时按放宽接受
                        _acc(accepted, floc, "field_added",
                             f"新增必填字段 {fname!r}（旧规则仅请求体顶层必填新增"
                             f"判破坏，嵌套层按放宽接受）",
                             direction)
                elif direction == DIRECTION_RESPONSE:
                    # 响应新增字段（即使必填）：旧客户端忽略多余字段，接受
                    _acc(accepted, floc, "field_added",
                         f"新增响应字段 {fname!r}"
                         f"{'（必填，旧客户端可安全忽略）' if fname in new_required else ''}",
                         direction)
                else:
                    _acc(accepted, floc, "optional_field_added",
                         f"新增可选字段 {fname!r}", direction)
        return changes

    # -- operation 级 --------------------------------------------------------

    def check(
        self, old_ir: ContractIR, new_ir: ContractIR, *, stamp_time: bool = True
    ) -> CompatibilityReport:
        accepted: list[AcceptedChange] = []
        breaking: list[BreakingChange] = []

        old_ops = {op.key: op for op in old_ir.operations}
        new_ops = {op.key: op for op in new_ir.operations}

        for key, op in old_ops.items():
            if key not in new_ops:
                breaking.append(BreakingChange(
                    key, "endpoint_removed", f"接口 {key} 被删除",
                    DIRECTION_ENDPOINT,
                ))
                continue
            breaking.extend(
                self._diff_operation(
                    op, new_ops[key], old_ir.raw, new_ir.raw, accepted
                )
            )

        for key in new_ops:
            if key not in old_ops:
                accepted.append(AcceptedChange(
                    key, "endpoint_added", "新增接口", DIRECTION_ENDPOINT
                ))

        # 规范化排序：相同输入 => 与执行顺序无关的稳定结论
        breaking.sort(key=lambda c: (c.location, c.kind, c.detail, c.direction))
        accepted.sort(key=lambda c: (c.location, c.kind, c.detail, c.direction))

        return CompatibilityReport(
            old_version=old_ir.version,
            new_version=new_ir.version,
            compatible=not breaking,
            rules_version=self.version,
            breaking_changes=breaking,
            accepted_changes=accepted,
            checked_at=(
                datetime.datetime.now(datetime.timezone.utc).isoformat()
                if stamp_time
                else ""
            ),
        )

    def _diff_operation(
        self,
        old_op,
        new_op,
        spec_old: dict,
        spec_new: dict,
        accepted: list[AcceptedChange],
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []
        loc = old_op.key

        # 参数（请求方向）：按 (in, name) 匹配
        old_params = {(p.location, p.name): p for p in old_op.parameters}
        new_params = {(p.location, p.name): p for p in new_op.parameters}
        for (where, pname), p in old_params.items():
            ploc = f"{loc} -> {where} 参数 '{pname}'"
            if (where, pname) not in new_params:
                changes.append(BreakingChange(
                    ploc, "parameter_removed",
                    f"{where} 参数 {pname!r} 被删除", DIRECTION_REQUEST,
                ))
                continue
            np = new_params[(where, pname)]
            if not p.required and np.required:
                changes.append(BreakingChange(
                    ploc, "required_added",
                    f"{where} 参数 {pname!r} 由可选变为必填", DIRECTION_REQUEST,
                ))
            elif p.required and not np.required:
                _acc(accepted, ploc, "required_relaxed",
                     "参数由必填变为可选，属于放宽", DIRECTION_REQUEST)
            changes.extend(
                self.diff_schemas(
                    p.schema_raw, np.schema_raw,
                    spec_old, spec_new, ploc, accepted, DIRECTION_REQUEST,
                )
            )
        for key in new_params:
            if key not in old_params:
                where, pname = key
                if new_params[key].required:
                    changes.append(BreakingChange(
                        f"{loc} -> {where} 参数 '{pname}'",
                        "required_added",
                        f"新增了必填 {where} 参数 {pname!r}", DIRECTION_REQUEST,
                    ))
                else:
                    _acc(accepted, f"{loc} -> {where} 参数 '{pname}'",
                         "optional_parameter_added", "新增可选参数",
                         DIRECTION_REQUEST)

        # 请求体（请求方向）
        if old_op.body is not None or new_op.body is not None:
            bloc = f"{loc} -> 请求体"
            if old_op.body is None and new_op.body is not None:
                if new_op.body.required:
                    changes.append(BreakingChange(
                        bloc, "required_added", "新增了必填请求体",
                        DIRECTION_REQUEST,
                    ))
                else:
                    _acc(accepted, bloc, "optional_body_added",
                         "新增可选请求体", DIRECTION_REQUEST)
            elif old_op.body is not None and new_op.body is None:
                changes.append(BreakingChange(
                    bloc, "body_removed", "请求体被删除", DIRECTION_REQUEST,
                ))
            else:
                ob, nb = old_op.body, new_op.body
                if not ob.required and nb.required:
                    changes.append(BreakingChange(
                        bloc, "required_added", "请求体由可选变为必填",
                        DIRECTION_REQUEST,
                    ))
                elif ob.required and not nb.required:
                    _acc(accepted, bloc, "required_relaxed",
                         "请求体由必填变为可选，属于放宽", DIRECTION_REQUEST)
                changes.extend(
                    self.diff_schemas(
                        ob.schema_raw, nb.schema_raw,
                        spec_old, spec_new, bloc, accepted, DIRECTION_REQUEST,
                        body_root=True,
                    )
                )

        # 响应（响应方向）
        old_resp = {r.status: r for r in old_op.responses}
        new_resp = {r.status: r for r in new_op.responses}
        for status, r in old_resp.items():
            rloc = f"{loc} -> 响应 {status}"
            if status not in new_resp:
                changes.append(BreakingChange(
                    rloc, "response_removed",
                    f"响应状态 {status} 被删除", DIRECTION_RESPONSE,
                ))
                continue
            nr = new_resp[status]
            if r.schema_raw and nr.schema_raw:
                changes.extend(
                    self.diff_schemas(
                        r.schema_raw, nr.schema_raw,
                        spec_old, spec_new, rloc, accepted, DIRECTION_RESPONSE,
                    )
                )
            elif r.schema_raw and not nr.schema_raw:
                changes.append(BreakingChange(
                    rloc, "response_removed",
                    f"响应 {status} 的响应体 schema 被删除", DIRECTION_RESPONSE,
                ))
        for status in new_resp:
            if status not in old_resp:
                _acc(accepted, f"{loc} -> 响应 {status}",
                     "response_status_added", "新增响应状态", DIRECTION_RESPONSE)

        return changes


# 规则注册表：复现历史判定时按版本取用；条目一经发布即冻结，不得就地修改。
_RULESETS: dict[str, _RuleSet] = {
    RULES_VERSION_2026_09: _RuleSet(
        RULES_VERSION_2026_09,
        response_required_relaxed=True,
        request_required_deep=False,
    ),
    RULES_VERSION: _RuleSet(
        RULES_VERSION,
        response_required_relaxed=False,
        request_required_deep=True,
    ),
}


def check_backward_compatible(
    old: ContractIR, new: ContractIR
) -> CompatibilityReport:
    """按当前 :data:`RULES_VERSION` 判定 new 是否向后兼容 old。"""
    return _RULESETS[RULES_VERSION].check(old, new)


def check_with_rules(
    old: ContractIR, new: ContractIR, rules_version: str
) -> CompatibilityReport:
    """按指定历史规则版本判定（复现历史结论，不带时间戳）。

    版本未注册时抛 :class:`UnknownRulesVersionError`（同时是 ``ValueError``），
    绝不静默回退到当前规则。
    """
    if rules_version not in _RULESETS:
        raise UnknownRulesVersionError(
            rules_version, available_rules_versions()
        )
    return _RULESETS[rules_version].check(old, new, stamp_time=False)


def compare_across_rules(
    old: ContractIR,
    new: ContractIR,
    versions: list[str] | tuple[str, ...] | None = None,
) -> dict[str, CompatibilityReport]:
    """一次跑多套冻结规则，便于规则升级前评估影响面。

    返回 ``{rules_version: report}``，按规则版本旧 -> 新排序；
    任一版本未注册即抛 :class:`UnknownRulesVersionError`，不做部分返回。
    """
    chosen = list(versions) if versions is not None else available_rules_versions()
    unknown = [v for v in chosen if v not in _RULESETS]
    if unknown:
        raise UnknownRulesVersionError(unknown[0], available_rules_versions())
    return {
        v: _RULESETS[v].check(old, new, stamp_time=False)
        for v in sorted(chosen)
    }
