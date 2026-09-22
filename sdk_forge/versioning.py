"""契约版本提取与向后兼容性判定。

判定规则由 :data:`RULES_VERSION` 标识；规则演进时提升版本号，历史判定
可凭归档契约与 rules_version 复现（见 :func:`replay_report`）。

兼容性规则摘要（consumer 视角，旧客户端 + 新服务）：

* 新增接口 / 新增可选请求参数 / 请求体新增可选字段 / 新增可选响应状态：接受
* 响应新增可选字段、枚举新增取值、类型放宽（int -> float）：接受
* 删除接口 / 参数 / 字段 / 响应状态、必填化、类型收窄、枚举收窄：拒绝
"""

from __future__ import annotations

import datetime
from typing import Any

from .models import (
    BreakingChange,
    CompatibilityReport,
    ContractIR,
)

# 当前规则集版本。规则发生变化时必须提升（保留旧版本函数用于复现历史判定）。
RULES_VERSION = "2026.09.01"

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


class _RuleSet:
    """单个规则版本的全部判定逻辑。"""

    def __init__(self, version: str) -> None:
        self.version = version

    # -- schema 级 -----------------------------------------------------------

    def diff_schemas(
        self,
        old_raw: dict,
        new_raw: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[str],
    ) -> list[BreakingChange]:
        """比较两处 schema（位置语义由 location 给出），返回破坏性变更。"""
        o = _normalize(old_raw, spec_old)
        n = _normalize(new_raw, spec_new)
        return self._diff_normalized(o, n, spec_old, spec_new, location, accepted)

    def _diff_normalized(
        self,
        o: dict,
        n: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[str],
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []

        ot, nt = _type_of(o), _type_of(n)

        # union
        if ot == "union" or nt == "union":
            changes.extend(
                self._diff_union(o, n, spec_old, spec_new, location, accepted)
            )
            return changes

        # 空 type：视为任意
        if ot is None and nt is not None:
            # 旧契约无约束 -> 新契约束窄为具体类型：破坏
            changes.append(BreakingChange(
                location, "type_narrowed",
                f"类型由无约束收窄为 {nt!r}",
            ))
            return changes
        if ot is not None and nt is None:
            accepted.append(f"{location}: 类型由 {ot!r} 放宽为无约束")
            return changes
        if ot is None and nt is None:
            return changes

        # nullability：旧可选 -> 新非空 是收窄（适用于字段/参数位置的联合判断）
        if o.get("x_nullable") and not n.get("x_nullable"):
            changes.append(BreakingChange(
                location, "type_narrowed",
                "由可空(nullable)变为非空，旧客户端可能收到/发送 null",
            ))
        elif not o.get("x_nullable") and n.get("x_nullable"):
            accepted.append(f"{location}: 变为可空(nullable)，属于放宽")

        # 标量类型放宽 / 收窄
        if ot != nt:
            allowed = _WIDENINGS.get(ot, set())
            if nt in allowed:
                accepted.append(f"{location}: 类型由 {ot!r} 放宽为 {nt!r}")
            else:
                changes.append(BreakingChange(
                    location, "type_narrowed",
                    f"类型由 {ot!r} 变更为 {nt!r}，不属于允许的放宽 "
                    f"({sorted(allowed) or '无'})",
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
                ))
            elif nf != of:
                accepted.append(f"{location}: format 由 {of!r} 放宽为 {nf!r}")

        # 枚举：新值必须是旧值超集
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
                ))
            if added and not removed:
                accepted.append(f"{location}: 枚举新增取值 {added}")

        # 对象字段
        if nt == "object" and ("properties" in o or "properties" in n):
            changes.extend(
                self._diff_object(o, n, spec_old, spec_new, location, accepted)
            )

        # 数组元素
        if nt == "array":
            oi, ni = o.get("items"), n.get("items")
            if oi is not None and ni is not None:
                changes.extend(
                    self.diff_schemas(
                        oi, ni, spec_old, spec_new,
                        f"{location}[]", accepted,
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
                        f"{location}.<value>", accepted,
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
        accepted: list[str],
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []
        if _type_of(o) != "union" or _type_of(n) != "union":
            # union <-> 具体类型：只允许 具体类型 -> 包含该类型的 union（放宽）
            if _type_of(o) != "union" and _type_of(n) == "union":
                if _type_of(o) in _union_types(n):
                    accepted.append(f"{location}: 具体类型扩展为联合类型")
                    return changes
            changes.append(BreakingChange(
                location, "type_narrowed",
                f"联合类型结构不兼容: {_type_of(o)!r} -> {_type_of(n)!r}",
            ))
            return changes
        old_members = {m.get("type"): m for m in o.get("x_union", [])}
        new_members = {m.get("type"): m for m in n.get("x_union", [])}
        removed = set(old_members) - set(new_members)
        added = set(new_members) - set(old_members)
        if removed:
            changes.append(BreakingChange(
                location, "enum_narrowed",
                f"联合类型删除了成员 {sorted(removed)}",
            ))
        if added:
            accepted.append(f"{location}: 联合类型新增成员 {sorted(added)}")
        return changes

    def _diff_object(
        self,
        o: dict,
        n: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[str],
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []
        old_props = o.get("properties", {})
        new_props = n.get("properties", {})
        old_required = set(o.get("required", []))
        new_required = set(n.get("required", []))

        for fname in old_props:
            floc = f"{location}.{fname}"
            if fname not in new_props:
                changes.append(BreakingChange(
                    floc, "field_removed", f"字段 {fname!r} 被删除",
                ))
                continue
            # 必填化是破坏；去必填是放宽
            if fname in old_required and fname not in new_required:
                accepted.append(f"{floc}: 字段由必填变为可选")
            elif fname not in old_required and fname in new_required:
                changes.append(BreakingChange(
                    floc, "required_added",
                    f"字段 {fname!r} 由可选变为必填",
                ))
            changes.extend(
                self.diff_schemas(
                    old_props[fname], new_props[fname],
                    spec_old, spec_new, floc, accepted,
                )
            )

        for fname in new_props:
            if fname not in old_props:
                if fname in new_required:
                    # 请求方向的必填新增在调用处另行处理；响应方向这不算破坏，
                    # 这里统一登记，由调用方按方向决定语义。
                    accepted.append(
                        f"{location}.{fname}: 新增字段（必填，"
                        f"在请求方向将按 required_added 处理）"
                    )
                else:
                    accepted.append(f"{location}.{fname}: 新增可选字段 {fname!r}")
        return changes

    # -- operation 级 --------------------------------------------------------

    def check(
        self, old_ir: ContractIR, new_ir: ContractIR, *, stamp_time: bool = True
    ) -> CompatibilityReport:
        accepted: list[str] = []
        breaking: list[BreakingChange] = []

        old_ops = {op.key: op for op in old_ir.operations}
        new_ops = {op.key: op for op in new_ir.operations}

        for key, op in old_ops.items():
            loc = key
            if key not in new_ops:
                breaking.append(BreakingChange(
                    loc, "endpoint_removed", f"接口 {key} 被删除",
                ))
                continue
            breaking.extend(
                self._diff_operation(
                    op, new_ops[key], old_ir.raw, new_ir.raw, accepted
                )
            )

        for key in new_ops:
            if key not in old_ops:
                accepted.append(f"{key}: 新增接口")

        compatible = not breaking
        return CompatibilityReport(
            old_version=old_ir.version,
            new_version=new_ir.version,
            compatible=compatible,
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
        accepted: list[str],
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []
        loc = old_op.key

        # 参数：按 (in, name) 匹配
        old_params = {(p.location, p.name): p for p in old_op.parameters}
        new_params = {(p.location, p.name): p for p in new_op.parameters}
        for (where, pname), p in old_params.items():
            ploc = f"{loc} -> {where} 参数 '{pname}'"
            if (where, pname) not in new_params:
                changes.append(BreakingChange(
                    ploc, "parameter_removed",
                    f"{where} 参数 {pname!r} 被删除",
                ))
                continue
            np = new_params[(where, pname)]
            # 请求方向：可选 -> 必填 破坏；必填 -> 可选 接受
            if not p.required and np.required:
                changes.append(BreakingChange(
                    ploc, "required_added",
                    f"{where} 参数 {pname!r} 由可选变为必填",
                ))
            elif p.required and not np.required:
                accepted.append(f"{ploc}: 由必填变为可选")
            changes.extend(
                self.diff_schemas(
                    p.schema_raw, np.schema_raw,
                    spec_old, spec_new, ploc, accepted,
                )
            )
        for key in new_params:
            if key not in old_params:
                where, pname = key
                if new_params[key].required:
                    changes.append(BreakingChange(
                        f"{loc} -> {where} 参数 '{pname}'",
                        "required_added",
                        f"新增了必填 {where} 参数 {pname!r}",
                    ))
                else:
                    accepted.append(
                        f"{loc} -> {where} 参数 '{pname}': 新增可选参数"
                    )

        # 请求体（consumer 发送方向：收窄/必填化破坏）
        if old_op.body is not None or new_op.body is not None:
            bloc = f"{loc} -> 请求体"
            if old_op.body is None and new_op.body is not None:
                if new_op.body.required:
                    changes.append(BreakingChange(
                        bloc, "required_added", "新增了必填请求体",
                    ))
                else:
                    accepted.append(f"{bloc}: 新增可选请求体")
            elif old_op.body is not None and new_op.body is None:
                changes.append(BreakingChange(
                    bloc, "body_removed", "请求体被删除",
                ))
            else:
                ob, nb = old_op.body, new_op.body
                if not ob.required and nb.required:
                    changes.append(BreakingChange(
                        bloc, "required_added", "请求体由可选变为必填",
                    ))
                elif ob.required and not nb.required:
                    accepted.append(f"{bloc}: 请求体由必填变为可选")
                # 请求方向：新增必填字段 = 破坏；删除字段 = 破坏
                changes.extend(
                    self._diff_request_body(
                        ob.schema_raw, nb.schema_raw,
                        spec_old, spec_new, bloc, accepted,
                    )
                )

        # 响应（consumer 接收方向：状态/字段删除破坏）
        old_resp = {r.status: r for r in old_op.responses}
        new_resp = {r.status: r for r in new_op.responses}
        for status, r in old_resp.items():
            rloc = f"{loc} -> 响应 {status}"
            if status not in new_resp:
                changes.append(BreakingChange(
                    rloc, "response_removed",
                    f"响应状态 {status} 被删除",
                ))
                continue
            nr = new_resp[status]
            if r.schema_raw and nr.schema_raw:
                changes.extend(
                    self.diff_schemas(
                        r.schema_raw, nr.schema_raw,
                        spec_old, spec_new, rloc, accepted,
                    )
                )
            elif r.schema_raw and not nr.schema_raw:
                changes.append(BreakingChange(
                    rloc, "response_removed",
                    f"响应 {status} 的响应体 schema 被删除",
                ))
        for status in new_resp:
            if status not in old_resp:
                accepted.append(f"{loc} -> 响应 {status}: 新增响应状态")

        return changes

    def _diff_request_body(
        self,
        old_raw: dict,
        new_raw: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[str],
    ) -> list[BreakingChange]:
        """请求方向：先收集方向特异规则（新增必填字段），再跑一次通用规则。

        通用规则（删字段、类型收窄、可选->必填 等）只执行一次，避免双重计数。
        """
        o = _normalize(old_raw, spec_old)
        n = _normalize(new_raw, spec_new)
        direction_changes: list[BreakingChange] = []
        if o.get("type") == "object" and n.get("type") == "object":
            old_props = o.get("properties", {})
            new_required = set(n.get("required", []))
            for fname in n.get("properties", {}):
                if fname not in old_props and fname in new_required:
                    direction_changes.append(BreakingChange(
                        f"{location}.{fname}", "required_added",
                        f"请求体新增必填字段 {fname!r}",
                    ))
        # 通用规则（含字段删除/必填化/类型与枚举收窄），只跑一次
        generic = self.diff_schemas(
            old_raw, new_raw, spec_old, spec_new, location, accepted
        )
        return direction_changes + generic


# 规则注册表：复现历史判定时按版本取用
_RULESETS: dict[str, _RuleSet] = {
    RULES_VERSION: _RuleSet(RULES_VERSION),
}


def check_backward_compatible(
    old: ContractIR, new: ContractIR
) -> CompatibilityReport:
    """按当前 :data:`RULES_VERSION` 判定 new 是否向后兼容 old。"""
    return _RULESETS[RULES_VERSION].check(old, new)


def check_with_rules(
    old: ContractIR, new: ContractIR, rules_version: str
) -> CompatibilityReport:
    """按指定历史规则版本判定（复现历史结论，不带时间戳）。"""
    if rules_version not in _RULESETS:
        raise ValueError(
            f"未知的规则版本 {rules_version!r}，可用: {sorted(_RULESETS)}"
        )
    return _RULESETS[rules_version].check(old, new, stamp_time=False)
