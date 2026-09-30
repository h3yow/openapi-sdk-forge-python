"""编排层：获取契约 -> 解析/校验 -> 兼容判定 -> 原子落盘。

稳定性保证：

* **并发去重（生成）**：同一契约版本的并发生成请求，只有一个执行；其余得到
  :class:`GenerationInProgressError`。生成完成后的重复请求直接返回已完成结果，
  绝不覆盖。
* **判定归档与幂等**：判定按「旧契约版本 + 新契约版本 + 规则版本」唯一归档。
  同一组合重复触发直接复用已完成结论，不重复计算、不覆盖；并发触发同一组合时，
  默认抛 :class:`JudgementInProgressError`，``wait=True`` 时等待并复用同一份结论。
  落盘仍是原子切换，过程中不会留下半成品，并发请求之间不会串结果。
* **原子落盘**：先写入同级临时目录并 fsync，再 ``os.replace`` 原子切换；
  任何失败都清理半成品，磁盘上只可能出现"上一版完整产物"或"本次完整产物"。
* **版本匹配**：调用方声明 ``expected_version`` 时与运行时版本严格比对，
  不一致抛 :class:`VersionMismatchError`。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

from .contract import fetch_openapi, parse_contract
from .errors import (
    ForgeError,
    GenerationInProgressError,
    IncompatibleContractError,
    JudgementInProgressError,
    UnknownRulesVersionError,
    VersionMismatchError,
)
from .generator import generate_sdk, render_sdk_zip
from .models import (
    AcceptedChange,
    CompatibilityReport,
    report_from_dict,
)
from .versioning import (
    RULES_VERSION,
    available_rules_versions,
    check_with_rules,
    compare_across_rules,
)

_CONTRACT_FILE = "contract.json"
_MANIFEST_FILE = "manifest.json"
_REPORT_FILE = "compatibility.json"

# 判定归档（与按版本的生成产物并列，集中存放，便于查询与列举）
_JUDGEMENTS_DIR = "_judgements"
_JUDGE_META_FILE = "meta.json"
_JUDGE_REPORT_FILE = "report.json"
_JUDGE_OLD_FILE = "old_contract.json"
_JUDGE_NEW_FILE = "new_contract.json"
# 没有历史契约（基线判定）时旧版本槽位的编码
_BASELINE_KEY = "__baseline__"


def _atomic_write_tree(target: Path, files: dict[str, bytes | str]) -> None:
    """把文件集合原子地写入 target 目录。

    target 已存在时整体替换；写入过程中 target 保持旧内容不变。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for rel, content in files.items():
            dst = tmp_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                dst.write_text(content, encoding="utf-8")
            else:
                dst.write_bytes(content)
        # fsync 内容，降低崩溃后半成品风险
        for root, _dirs, names in os.walk(tmp_dir):
            for name in names:
                fd = os.open(Path(root) / name, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        if target.exists():
            old_backup = target.with_name(target.name + ".old")
            if old_backup.exists():
                shutil.rmtree(old_backup)
            os.replace(target, old_backup)
            try:
                os.replace(tmp_dir, target)
            except BaseException:
                os.replace(old_backup, target)
                raise
            shutil.rmtree(old_backup, ignore_errors=True)
        else:
            os.replace(tmp_dir, target)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


class ForgeService:
    """带并发去重与归档状态的编排服务。"""

    # 版本未知前（拉取契约阶段）使用的临时槽位
    _FETCH_SLOT = "__fetching__"

    def __init__(self) -> None:
        # key -> 线程锁；锁存在即代表"生成进行中"。
        # key 为契约版本；拉取契约（尚不知版本）时用 _FETCH_SLOT/<base_url>。
        self._inflight: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        # 判定去重：judge_key -> 进行中条目的同步原语与结果
        self._judging: dict[str, dict] = {}

    def _acquire(self, slot: str) -> threading.Lock | None:
        """尝试占用某槽位；已被占用时返回 None。"""
        with self._guard:
            lock = self._inflight.get(slot)
            if lock is not None:
                return None
            lock = threading.Lock()
            lock.acquire()
            self._inflight[slot] = lock
            return lock

    def _move_slot(self, old: str, new: str) -> bool:
        """把占用从 old 迁移到 new；new 已被占用时保持 old 不变并返回 False。"""
        with self._guard:
            if new in self._inflight:
                return False
            if old not in self._inflight:
                return False
            self._inflight[new] = self._inflight.pop(old)
            return True

    def _release(self, slot: str) -> None:
        with self._guard:
            lock = self._inflight.pop(slot, None)
        if lock is not None:
            lock.release()

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    def generate(
        self,
        base_url: str,
        output_dir: Path,
        *,
        expected_version: str | None = None,
        timeout: float = 5.0,
        package_name: str = "generated_sdk",
        include_tags: set[str] | None = None,
    ) -> dict:
        output_dir = Path(output_dir)

        # 指定了版本且本地已有该版本的完整产物：直接复用，不访问网络、不覆盖
        if expected_version is not None:
            manifest_path = output_dir / expected_version / _MANIFEST_FILE
            if manifest_path.exists():
                return self._load_existing(output_dir / expected_version)

        # 进入流程即占槽（此时版本未知，按目标服务占槽），
        # 保证"并发重复触发"在拉取契约阶段也会被立即拒绝
        fetch_slot = f"{self._FETCH_SLOT}:{base_url}"
        lock = self._acquire(fetch_slot)
        if lock is None:
            raise GenerationInProgressError(f"target {base_url}")
        current_slot = fetch_slot
        try:
            # 网络获取 + 解析（失败不触碰磁盘）
            spec = fetch_openapi(base_url, timeout=timeout)
            ir = parse_contract(spec, include_tags=include_tags)

            if expected_version is not None and ir.version != expected_version:
                raise VersionMismatchError(expected_version, ir.version)

            version_key = ir.version or "_unversioned"

            # 迁移到版本槽：同版本的另一路任务已在生成则拒绝
            if not self._move_slot(fetch_slot, version_key):
                raise GenerationInProgressError(version_key)
            current_slot = version_key

            # 拿到版本槽后检查是否已存在完整产物（可能由更早完成的任务写入）
            manifest_path = output_dir / version_key / _MANIFEST_FILE
            if manifest_path.exists():
                return self._load_existing(output_dir / version_key)

            files_text = generate_sdk(ir, package_name=package_name)
            files: dict[str, bytes | str] = {
                _CONTRACT_FILE: json.dumps(spec, ensure_ascii=False, indent=2),
                _MANIFEST_FILE: json.dumps(
                    {
                        "title": ir.title,
                        "version": ir.version,
                        "rules_version": RULES_VERSION,
                        "package_name": package_name,
                        "include_tags": sorted(include_tags) if include_tags else None,
                        "files": sorted(files_text),
                        "base_url": base_url,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            }
            for rel, content in files_text.items():
                files[rel] = content
            files["sdk.zip"] = render_sdk_zip(files_text)

            _atomic_write_tree(output_dir / version_key, files)
            return {
                "status": "generated",
                "version": ir.version,
                "directory": str(output_dir / version_key),
                "files": sorted(files_text) + ["sdk.zip", _CONTRACT_FILE, _MANIFEST_FILE],
            }
        finally:
            self._release(current_slot)

    @staticmethod
    def _load_existing(version_dir: Path) -> dict:
        manifest = json.loads((version_dir / _MANIFEST_FILE).read_text())
        return {
            "status": "already_exists",
            "version": manifest.get("version"),
            "directory": str(version_dir),
            "files": sorted(
                str(p.relative_to(version_dir))
                for p in version_dir.rglob("*")
                if p.is_file()
            ),
        }

    # ------------------------------------------------------------------
    # 兼容性判定（归档 / 去重 / 并发 / 查询）
    # ------------------------------------------------------------------

    @staticmethod
    def _judge_key(
        old_version: str | None, new_version: str | None, rules_version: str
    ) -> str:
        ov = old_version or _BASELINE_KEY
        nv = new_version or "_unversioned"
        return f"{quote(ov, safe='')}__{quote(nv, safe='')}__{quote(rules_version, safe='')}"

    def _judgement_dir(
        self,
        output_dir: Path,
        old_version: str | None,
        new_version: str | None,
        rules_version: str,
    ) -> Path:
        return (
            Path(output_dir)
            / _JUDGEMENTS_DIR
            / self._judge_key(old_version, new_version, rules_version)
        )

    @staticmethod
    def _load_judgement(dirpath: Path) -> CompatibilityReport:
        return report_from_dict(
            json.loads((dirpath / _JUDGE_REPORT_FILE).read_text())
        )

    def _try_load_archived(
        self,
        output_dir: Path,
        old_version: str | None,
        new_version: str | None,
        rules_version: str,
    ) -> CompatibilityReport | None:
        report_file = (
            self._judgement_dir(output_dir, old_version, new_version, rules_version)
            / _JUDGE_REPORT_FILE
        )
        return self._load_judgement(report_file.parent) if report_file.exists() else None

    def judge(
        self,
        *,
        old_contract: dict | None,
        new_contract: dict,
        output_dir: Path,
        rules_version: str = RULES_VERSION,
        wait: bool = False,
    ) -> CompatibilityReport:
        """对一对契约执行判定并归档；幂等且并发安全。

        * ``old_contract=None`` 表示基线判定（无历史契约）。
        * 相同（旧版本, 新版本, 规则版本）已有归档：直接返回，不重复计算。
        * 相同组合正在进行：``wait=False`` 抛 :class:`JudgementInProgressError`；
          ``wait=True`` 阻塞到对方完成后复用同一份结论（对方失败则同步抛出）。
        * 归档目录按组合隔离 + 原子写入：不会留下半成品，不会互相写坏。
        """
        output_dir = Path(output_dir)
        if rules_version not in available_rules_versions():
            raise UnknownRulesVersionError(rules_version, available_rules_versions())

        new_ir = parse_contract(new_contract)
        old_ir = parse_contract(old_contract) if old_contract is not None else None
        old_version = old_ir.version if old_ir is not None else None
        new_version = new_ir.version
        key = self._judge_key(old_version, new_version, rules_version)
        target_dir = output_dir / _JUDGEMENTS_DIR / key

        # 快路径：已归档，直接复用（不触碰任何进行中状态）
        if (target_dir / _JUDGE_REPORT_FILE).exists():
            return self._load_judgement(target_dir)

        is_worker = False
        with self._guard:
            # 双重检查：拿锁前可能已由别的线程落盘
            if (target_dir / _JUDGE_REPORT_FILE).exists():
                return self._load_judgement(target_dir)
            inflight = self._judging.get(key)
            if inflight is not None:
                if not wait:
                    raise JudgementInProgressError(key)
                holder = inflight  # 本地持有：worker 弹出槽位后仍能取到结果
            else:
                holder = {
                    "event": threading.Event(),
                    "report": None,
                    "error": None,
                }
                self._judging[key] = holder
                is_worker = True

        if not is_worker:
            # 等待方：复用 worker 结论，绝不重复计算
            holder["event"].wait()
            if holder["report"] is not None:
                return holder["report"]
            # worker 成功落盘（槽位已弹出）也能直接取回
            if (target_dir / _JUDGE_REPORT_FILE).exists():
                return self._load_judgement(target_dir)
            # 抛出与 worker 同类的可区分错误，避免静默按成功处理
            if holder["error"] is not None:
                raise holder["error"]
            raise JudgementInProgressError(key)
        # worker 路径：唯一负责计算与落盘
        try:
            report = self._compute_judgement(
                old_ir, new_ir, rules_version, output_dir,
                old_contract, new_contract,
                old_version, new_version, target_dir,
            )
            holder["report"] = report
            return report
        except BaseException as exc:
            holder["error"] = exc
            raise
        finally:
            with self._guard:
                self._judging.pop(key, None)
            # 结果已写入本地 holder；弹出槽位后再唤醒，等待方凭 holder 必能读到结论
            holder["event"].set()

    def _compute_judgement(
        self,
        old_ir,
        new_ir,
        rules_version: str,
        output_dir: Path,
        old_contract: dict | None,
        new_contract: dict,
        old_version: str | None,
        new_version: str | None,
        target_dir: Path,
    ) -> CompatibilityReport:
        if old_ir is None:
            report = CompatibilityReport(
                old_version=None,
                new_version=new_version,
                compatible=True,
                rules_version=rules_version,
                accepted_changes=[AcceptedChange(
                    location="(baseline)",
                    kind="baseline_registered",
                    detail="无历史归档契约，当前版本作为兼容性基线",
                    direction="endpoint",
                )],
            )
        else:
            # 归档判定一律不盖时间戳：相同输入逐字节一致，可任意复现
            report = check_with_rules(old_ir, new_ir, rules_version)

        meta = {
            "old_version": old_version,
            "new_version": new_version,
            "rules_version": rules_version,
            "compatible": report.compatible,
        }
        files: dict[str, str] = {
            _JUDGE_META_FILE: json.dumps(meta, ensure_ascii=False, indent=2,
                                         sort_keys=True),
            _JUDGE_REPORT_FILE: json.dumps(
                report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
            ),
            _JUDGE_NEW_FILE: json.dumps(
                new_contract, ensure_ascii=False, indent=2, sort_keys=True
            ),
        }
        if old_contract is not None:
            files[_JUDGE_OLD_FILE] = json.dumps(
                old_contract, ensure_ascii=False, indent=2, sort_keys=True
            )
        _atomic_write_tree(target_dir, files)
        return report

    def check_compatibility(
        self,
        base_url: str,
        output_dir: Path,
        *,
        timeout: float = 5.0,
        raise_on_breaking: bool = False,
        rules_version: str = RULES_VERSION,
    ) -> CompatibilityReport:
        output_dir = Path(output_dir)
        spec = fetch_openapi(base_url, timeout=timeout)
        new_ir = parse_contract(spec)
        version_key = new_ir.version or "_unversioned"

        # 找上一份归档契约（排除当前版本）
        old_spec = self._latest_archived_contract(output_dir, exclude=version_key)

        # 走统一的判定管线：幂等、并发去重、原子归档到 _judgements/
        report = self.judge(
            old_contract=old_spec,
            new_contract=spec,
            output_dir=output_dir,
            rules_version=rules_version,
            wait=True,
        )

        # 兼容旧布局：按版本目录再镜像一份报告（整体原子写，永不覆盖为半成品）
        report_dir = output_dir / version_key / "checks" / rules_version
        _atomic_write_tree(
            report_dir,
            {_REPORT_FILE: json.dumps(report.to_dict(), ensure_ascii=False,
                                      indent=2, sort_keys=True)},
        )

        if raise_on_breaking and not report.compatible:
            raise IncompatibleContractError(
                [c.format() for c in report.breaking_changes]
            )
        return report

    def get_judgement(
        self,
        *,
        output_dir: Path,
        new_version: str,
        rules_version: str,
        old_version: str | None = None,
    ) -> CompatibilityReport:
        """按契约版本 + 规则版本取回已归档判定；不存在返回 None。"""
        found = self._try_load_archived(
            Path(output_dir), old_version, new_version, rules_version
        )
        if found is None:
            raise KeyError(
                self._judge_key(old_version, new_version, rules_version)
            )
        return found

    def list_judgements(self, output_dir: Path) -> list[dict]:
        """列出全部已归档判定记录的摘要（按 key 排序，顺序稳定）。"""
        root = Path(output_dir) / _JUDGEMENTS_DIR
        if not root.exists():
            return []
        records: list[dict] = []
        for d in sorted(root.iterdir(), key=lambda p: p.name):
            meta_path = d / _JUDGE_META_FILE
            if d.is_dir() and meta_path.exists():
                records.append(json.loads(meta_path.read_text()))
        return records

    @staticmethod
    def _latest_archived_contract(output_dir: Path, *, exclude: str) -> dict | None:
        if not output_dir.exists():
            return None
        candidates = [
            d for d in output_dir.iterdir()
            if d.is_dir() and (d / _CONTRACT_FILE).exists() and d.name != exclude
        ]
        if not candidates:
            return None

        def sort_key(d: Path) -> tuple:
            # 版本号按点分数字段比较，非数字段退化为 (1, 字符串)
            parts = []
            for seg in d.name.split("."):
                parts.append((0, int(seg)) if seg.isdigit() else (1, seg))
            return tuple(parts)

        candidates.sort(key=sort_key)
        return json.loads((candidates[-1] / _CONTRACT_FILE).read_text())

    def replay(
        self,
        *,
        old_contract: dict,
        new_contract: dict,
        rules_version: str | None = None,
    ) -> CompatibilityReport:
        """用历史契约（及可选的历史规则版本）复现判定。

        纯计算、不落盘、不盖时间戳：相同契约 + 相同规则版本 => 完全一致的报告。
        未注册的规则版本抛 :class:`UnknownRulesVersionError`（``ValueError`` 子类）。
        """
        old_ir = parse_contract(old_contract)
        new_ir = parse_contract(new_contract)
        return check_with_rules(
            old_ir, new_ir, rules_version or RULES_VERSION
        )

    def compare_rules(
        self,
        *,
        old_contract: dict | None,
        new_contract: dict,
        versions: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, CompatibilityReport]:
        """同一份契约演进在各冻结规则版本下分别判成什么（影响面评估）。

        返回 ``{rules_version: report}``（旧规则 -> 新规则排序）；
        ``old_contract=None`` 时所有版本都得到兼容基线。
        """
        old_ir = parse_contract(old_contract) if old_contract is not None else None
        new_ir = parse_contract(new_contract)
        if old_ir is None:
            chosen = (
                list(versions) if versions is not None
                else available_rules_versions()
            )
            unknown = [v for v in chosen if v not in available_rules_versions()]
            if unknown:
                raise UnknownRulesVersionError(
                    unknown[0], available_rules_versions()
                )
            return {
                v: CompatibilityReport(
                    old_version=None,
                    new_version=new_ir.version,
                    compatible=True,
                    rules_version=v,
                    accepted_changes=[AcceptedChange(
                        location="(baseline)",
                        kind="baseline_registered",
                        detail="无历史归档契约，当前版本作为兼容性基线",
                        direction="endpoint",
                    )],
                )
                for v in sorted(chosen)
            }
        return compare_across_rules(old_ir, new_ir, versions)


# 模块级便捷接口（保留 service.py 早期骨架的 API 形状）
_default_service = ForgeService()


def generate_for_service(
    base_url: str,
    *,
    output_dir: Path,
    expected_version: str | None = None,
    timeout: float = 5.0,
    include_tags: set[str] | None = None,
) -> dict:
    return _default_service.generate(
        base_url, Path(output_dir),
        expected_version=expected_version, timeout=timeout,
        include_tags=include_tags,
    )


def check_service_compatibility(
    base_url: str,
    *,
    output_dir: Path,
    timeout: float = 5.0,
) -> CompatibilityReport:
    return _default_service.check_compatibility(
        base_url, Path(output_dir), timeout=timeout
    )


def replay_report(
    *,
    output_dir: Path,
    old_contract: dict,
    new_contract: dict,
    rules_version: str | None = None,
) -> CompatibilityReport:
    return _default_service.replay(
        old_contract=old_contract,
        new_contract=new_contract,
        rules_version=rules_version,
    )


def judge_contracts(
    *,
    old_contract: dict | None,
    new_contract: dict,
    output_dir: Path,
    rules_version: str | None = None,
    wait: bool = False,
) -> CompatibilityReport:
    """执行并归档判定（幂等、并发去重）。"""
    return _default_service.judge(
        old_contract=old_contract,
        new_contract=new_contract,
        output_dir=Path(output_dir),
        rules_version=rules_version or RULES_VERSION,
        wait=wait,
    )


def list_judgements(output_dir: Path) -> list[dict]:
    """列举已归档判定记录摘要。"""
    return _default_service.list_judgements(Path(output_dir))


def get_judgement(
    *,
    output_dir: Path,
    new_version: str,
    rules_version: str,
    old_version: str | None = None,
) -> CompatibilityReport:
    """按契约版本 + 规则版本取回已归档判定。"""
    return _default_service.get_judgement(
        output_dir=Path(output_dir),
        new_version=new_version,
        rules_version=rules_version,
        old_version=old_version,
    )


def compare_rules_for_contracts(
    *,
    old_contract: dict | None,
    new_contract: dict,
    versions: list[str] | tuple[str, ...] | None = None,
) -> dict[str, CompatibilityReport]:
    """同一份演进跨规则版本对比（不落盘）。"""
    return _default_service.compare_rules(
        old_contract=old_contract, new_contract=new_contract, versions=versions
    )
