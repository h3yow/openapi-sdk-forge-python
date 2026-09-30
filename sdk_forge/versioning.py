"""契约版本提取与按数据方向分离的向后兼容性判定。

判定规则由 :data:`RULES_VERSION` 标识；规则演进时提升版本号并在
:data:`RULESETS` 注册表中保留旧版本实现，历史判定可凭归档契约与
``rules_version`` 字节级复现（见 :func:`check_with_rules` /
:func:`compare_across_rules`）。

数据方向（consumer = 旧客户端 + 新服务 视角）：

* **请求方向**（参数 / 请求体，客户端 -> 服务端）：
  新增必填参数或字段、删除参数或字段、类型/枚举收窄都判破坏。
* **响应方向**（响应状态 / 响应字段，服务端 -> 客户端）：
  字段必填 -> 可选判破坏（服务端可能不再返回，老客户端读到缺失数据）；
  字段可选 -> 必填接受（服务端给出更强保证）；删除响应字段、类型/枚举
  收窄仍判破坏。

规则集版本：

* ``2026.09.01``（已冻结）：方向不敏感的早期规则——字段/参数必填变可选
  一律接受，新增必填字段仅在**请求体顶层**判破坏。保留该实现用于复现
  历史结论，永不改动。
* ``2026.09.30``（当前 :data:`RULES_VERSION`）：请求/响应方向分别判定；
  请求体任意深度的新增必填字段都判破坏；响应必填字段变可选判破坏
  （新 kind：``field_became_optional``），可选变必填接受。
"""

from __future__ import annotations

import datetime
from typing import Any

from .errors import UnknownRulesVersionError
from .models import (
    BreakingChange,
    CompatibilityReport,
    ContractIR,
)

# 历史规则版本（已冻结，禁止改动其判定逻辑）。
RULES_VERSION_V1 = "2026.09.01"
# 当前规则集版本。规则发生变化时必须提升并在此注册新版本。
RULES_VERSION = "2026.09.30"

# 数据方向标识（写入每条 BreakingChange）。
DIRECTION_REQUEST = "request"
DIRECTION_RESPONSE = "response"
DIRECTION_ENDPOINT = "endpoint"

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
    """单个规则版本的全部判定逻辑（基类 = 当前规则，按方向分别判定）。

    子类通过覆写三个方向钩子表达版本差异；冻结版本（见
    :class:`_FrozenRulesV1`）覆写后不得再改，以保证历史结论可复现。
    """

    version: str = RULES_VERSION

    def __init__(self, version: str | None = None) -> None:
        if version is not None:
            self.version = version

    # -- 方向钩子（新版本语义；冻结版本在子类中覆写） -------------------------

    def required_to_optional(
        self, direction: str, depth: int, *, subject: str, name: str
    ) -> tuple[str, str] | None:
        """必填 -> 可选。返回 (kind, detail) 表示破坏；None 表示接受。"""
        if direction == DIRECTION_RESPONSE:
            return (
                "field_became_optional",
                (
                    f"{subject}由必填变为可选：服务端可能不再返回该字段，"
                    "老客户端会读到缺失数据"
                ),
            )
        # 请求方向：客户端不再被强制发送，属于放宽
        return None

    def optional_to_required(
        self, direction: str, depth: int, *, subject: str, name: str
    ) -> tuple[str, str] | None:
        """可选 -> 必填。返回 (kind, detail) 表示破坏；None 表示接受。"""
        if direction == DIRECTION_REQUEST:
            return (
                "required_added",
                f"{subject} 由可选变为必填，旧客户端不会提供该值",
            )
        # 响应方向：服务端给出更强保证，接受
        return None

    def new_required_field(
        self, direction: str, depth: int, *, subject: str, name: str
    ) -> tuple[str, str] | None:
        """新增的字段在新契约中标记为必填。返回破坏描述或 None。"""
        if direction == DIRECTION_REQUEST:
            return (
                "required_added",
                f"新增了必填字段 {name!r}，旧客户端不会提供该值",
            )
        return None

    # -- schema 级 -----------------------------------------------------------

    def diff_schemas(
        self,
        old_raw: dict,
        new_raw: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[str],
        *,
        direction: str = DIRECTION_REQUEST,
        depth: int = 0,
    ) -> list[BreakingChange]:
        """比较两处 schema（位置语义由 location/direction 给出）。

        ``depth`` 为相对操作根 schema 的对象嵌套深度（参数/请求体/响应体
        根为 0，数组元素不增加深度），供冻结版本区分"顶层 vs 嵌套"语义。
        """
        o = _normalize(old_raw, spec_old)
        n = _normalize(new_raw, spec_new)
        return self._diff_normalized(
            o, n, spec_old, spec_new, location, accepted,
            direction=direction, depth=depth,
        )

    def _diff_normalized(
        self,
        o: dict,
        n: dict,
        spec_old: dict,
        spec_new: dict,
        location: str,
        accepted: list[str],
        *,
        direction: str,
        depth: int,
    ) -> list[BreakingChange]:
        changes: list[BreakingChange] = []

        ot, nt = _type_of(o), _type_of(n)

        # union
        if ot == "union" or nt == "union":
            changes.extend(
                self._diff_union(
                    o, n, spec_old, spec_new, location, accepted,
                    direction=direction,
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
            accepted.append(f"{location}: 类型由 {ot!r} 放宽为无约束")
            return changes
        if ot is None and nt is None:
            return changes

        # nullability：旧可选 -> 新非空 是收窄（两个方向均破坏：
        # 请求侧旧客户端可能发 null；响应侧旧客户端已按可空处理数据）
        if o.get("x_nullable") and not n.get("x_nullable"):
            changes.append(BreakingChange(
                location, "type_narrowed",
                "由可空(nullable)变为非空，旧客户端可能收到/发送 null",
                direction,
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
                accepted.append(f"{location}: format 由 {of!r} 放宽为 {nf!r}")

        # 枚举：新值必须是旧值超集（两个方向同理：取值集合不得缩小）
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
                accepted.append(f"{location}: 枚举新增取值 {added}")

        # 对象字段
        if nt == "object" and ("properties" in o or "properties" in n):
            changes.extend(
                self._diff_object(o, n, spec_old, spec_new, location, accepted,
                                  direction=direction, depth=depth)
            )

        # 数组元素（数组本身不增加对象嵌套深度）
        if nt == "array":
            oi, ni = o.get("items"), n.get("items")
            if oi is not None and ni is not None:
                changes.extend(
                    self.diff_schemas(
                        oi, ni, spec_old, spec_new,
                        f"{location}[]", accepted,
                        direction=direction, depth=depth,
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
                        direction=direction, depth=depth,
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
        *,
        direction: str = DIRECTION_REQUEST,
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
        *,
        direction: str,
        depth: int,
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
                    floc, "field_removed", f"字段 {fname!r} 被删除", direction,
                ))
                continue
            # 必填约束翻转：语义随方向/规则版本变化
            if fname in old_required and fname not in new_required:
                hit = self.required_to_optional(
                    direction, depth, subject="字段", name=fname
                )
                if hit is None:
                    accepted.append(f"{floc}: 字段由必填变为可选")
                else:
                    kind, detail = hit
                    changes.append(BreakingChange(floc, kind, detail, direction))
            elif fname not in old_required and fname in new_required:
                hit = self.optional_to_required(
                    direction, depth, subject="字段", name=fname
                )
                if hit is None:
                    accepted.append(f"{floc}: 字段由可选变为必填")
                else:
                    kind, detail = hit
                    changes.append(BreakingChange(floc, kind, detail, direction))
            changes.extend(
                self.diff_schemas(
                    old_props[fname], new_props[fname],
                    spec_old, spec_new, floc, accepted,
                    direction=direction, depth=depth + 1,
                )
            )

        for fname in new_props:
            if fname not in old_props:
                floc = f"{location}.{fname}"
                if fname in new_required:
                    hit = self.new_required_field(
                        direction, depth, subject="字段", name=fname
                    )
                    if hit is None:
                        accepted.append(f"{floc}: 新增必填字段 {fname!r}")
                    else:
                        kind, detail = hit
                        changes.append(
                            BreakingChange(floc, kind, detail, direction)
                        )
                else:
                    accepted.append(f"{floc}: 新增可选字段 {fname!r}")
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
                accepted.append(f"{key}: 新增接口")

        # 稳定排序：结论顺序只取决于 (接口, 位置, 类别, 依据)，与
        # dict 迭代顺序/执行顺序无关，保证逐字节复现。
        breaking.sort(key=lambda c: (c.location, c.kind, c.direction, c.detail))
        accepted.sort()

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
            if p.required and not np.required:
                hit = self.required_to_optional(
                    DIRECTION_REQUEST, 0, subject=f"{where}参数", name=pname
                )
                if hit is None:
                    accepted.append(f"{ploc}: 由必填变为可选")
                else:
                    kind, detail = hit
                    changes.append(
                        BreakingChange(ploc, kind, detail, DIRECTION_REQUEST)
                    )
            elif not p.required and np.required:
                hit = self.optional_to_required(
                    DIRECTION_REQUEST, 0, subject=f"{where} 参数", name=pname
                )
                if hit is None:
                    accepted.append(f"{ploc}: 由可选变为必填")
                else:
                    kind, detail = hit
                    changes.append(
                        BreakingChange(ploc, kind, detail, DIRECTION_REQUEST)
                    )
            changes.extend(
                self.diff_schemas(
                    p.schema_raw, np.schema_raw,
                    spec_old, spec_new, ploc, accepted,
                    direction=DIRECTION_REQUEST,
                )
            )
        for key in new_params:
            if key not in old_params:
                where, pname = key
                ploc = f"{loc} -> {where} 参数 '{pname}'"
                if new_params[key].required:
                    changes.append(BreakingChange(
                        ploc, "required_added",
                        f"新增了必填 {where} 参数 {pname!r}", DIRECTION_REQUEST,
                    ))
                else:
                    accepted.append(f"{ploc}: 新增可选参数")

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
                    accepted.append(f"{bloc}: 新增可选请求体")
            elif old_op.body is not None and new_op.body is None:
                changes.append(BreakingChange(
                    bloc, "body_removed", "请求体被删除", DIRECTION_REQUEST,
                ))
            else:
                ob, nb = old_op.body, new_op.body
                # 请求体本身的必填翻转：两个规则版本结论一致（请求方向）
                if ob.required and not nb.required:
                    accepted.append(f"{bloc}: 请求体由必填变为可选")
                elif not ob.required and nb.required:
                    changes.append(BreakingChange(
                        bloc, "required_added", "请求体由可选变为必填",
                        DIRECTION_REQUEST,
                    ))
                changes.extend(
                    self._diff_request_body(
                        ob.schema_raw, nb.schema_raw,
                        spec_old, spec_new, bloc, accepted,
                    )
                )

        # 响应（响应方向）：状态/字段删除、必填->可选、类型收窄均破坏
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
                        spec_old, spec_new, rloc, accepted,
                        direction=DIRECTION_RESPONSE,
                    )
                )
            elif r.schema_raw and not nr.schema_raw:
                changes.append(BreakingChange(
                    rloc, "response_removed",
                    f"响应 {status} 的响应体 schema 被删除", DIRECTION_RESPONSE,
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
        """请求方向的对象 schema 比较。

        通用规则（删字段、必填翻转、类型与枚举收窄、新增必填字段）全部由
        :meth:`diff_schemas` 配合方向钩子执行一次，避免双重计数。
        """
        return self.diff_schemas(
            old_raw, new_raw, spec_old, spec_new, location, accepted,
            direction=DIRECTION_REQUEST,
        )


class _FrozenRulesV1(_RuleSet):
    """冻结的 2026.09.01 规则：方向不敏感的早期语义。

    * 字段/参数必填 -> 可选：两个方向都接受；
    * 字段可选 -> 必填：仅请求方向破坏（参数级在 operation 比较中另行处理）；
    * 新增必填字段：只有**请求体顶层对象**判破坏，嵌套对象中一律接受
      （保持与冻结时 ``_diff_request_body`` 的历史行为一致）。
    """

    version = RULES_VERSION_V1

    def required_to_optional(self, direction: str, depth: int, *, subject: str, name: str):
        return None  # 必填 -> 可选，历史上一律接受

    def optional_to_required(self, direction: str, depth: int, *, subject: str, name: str):
        if direction == DIRECTION_REQUEST:
            return ("required_added", f"{subject} {name!r} 由可选变为必填")
        return None

    def new_required_field(self, direction: str, depth: int, *, subject: str, name: str):
        if direction == DIRECTION_REQUEST and depth == 0:
            return (
                "required_added",
                f"请求体新增必填字段 {name!r}",
            )
        return None


# 规则注册表：复现历史判定时按版本取用；冻结版本登记后不得修改其实现。
RULESETS: dict[str, _RuleSet] = {
    RULES_VERSION_V1: _FrozenRulesV1(),
    RULES_VERSION: _RuleSet(RULES_VERSION),
}


def available_rules_versions() -> list[str]:
    """返回全部已注册规则版本（旧 -> 新）。"""
    return sorted(RULESETS)


def _get_ruleset(rules_version: str) -> _RuleSet:
    if rules_version not in RULESETS:
        raise UnknownRulesVersionError(
            rules_version, sorted(RULESETS)
        )
    return RULESETS[rules_version]


def check_backward_compatible(
    old: ContractIR, new: ContractIR
) -> CompatibilityReport:
    """按当前 :data:`RULES_VERSION` 判定 new 是否向后兼容 old。"""
    return RULESETS[RULES_VERSION].check(old, new)


def check_with_rules(
    old: ContractIR, new: ContractIR, rules_version: str
) -> CompatibilityReport:
    """按指定（含历史冻结）规则版本判定（复现历史结论，不带时间戳）。

    规则版本不存在时抛 :class:`UnknownRulesVersionError`（同时是
    :class:`ValueError`），绝不静默回退到当前规则。
    """
    return _get_ruleset(rules_version).check(old, new, stamp_time=False)


def compare_across_rules(
    old: ContractIR,
    new: ContractIR,
    rules_versions: list[str] | None = None,
) -> dict[str, CompatibilityReport]:
    """一次跑出同一份契约演进在多个规则版本下的结论（规则升级影响面评估）。

    :return: ``{rules_version: CompatibilityReport}``，均不带时间戳，
        可逐字段比较；任一版本未注册即抛 :class:`UnknownRulesVersionError`。
    """
    versions = rules_versions or available_rules_versions()
    return {v: check_with_rules(old, new, v) for v in versions}
