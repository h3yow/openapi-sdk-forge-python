"""编排层：获取契约 -> 解析/校验 -> 兼容判定 -> 原子落盘。

稳定性保证：

* **并发去重**：同一契约版本的并发生成请求，只有一个执行；其余得到
  :class:`GenerationInProgressError`。生成完成后的重复请求直接返回已完成结果，
  绝不覆盖。
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

from .contract import fetch_openapi, parse_contract
from .errors import (
    ForgeError,
    GenerationInProgressError,
    IncompatibleContractError,
    VersionMismatchError,
)
from .generator import generate_sdk, render_sdk_zip
from .models import CompatibilityReport
from .versioning import (
    RULES_VERSION,
    check_backward_compatible,
    check_with_rules,
)

_CONTRACT_FILE = "contract.json"
_MANIFEST_FILE = "manifest.json"
_REPORT_FILE = "compatibility.json"


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
    # 兼容性
    # ------------------------------------------------------------------

    def check_compatibility(
        self,
        base_url: str,
        output_dir: Path,
        *,
        timeout: float = 5.0,
        raise_on_breaking: bool = False,
    ) -> CompatibilityReport:
        output_dir = Path(output_dir)
        spec = fetch_openapi(base_url, timeout=timeout)
        new_ir = parse_contract(spec)
        version_key = new_ir.version or "_unversioned"

        # 找上一份归档契约（排除当前版本）
        old_spec = self._latest_archived_contract(output_dir, exclude=version_key)
        if old_spec is None:
            # 没有历史版本：记录基线并视为兼容
            report = CompatibilityReport(
                old_version=None,
                new_version=new_ir.version,
                compatible=True,
                rules_version=RULES_VERSION,
                accepted_changes=["无历史归档契约，当前版本作为兼容性基线"],
            )
        else:
            old_ir = parse_contract(old_spec)
            report = check_backward_compatible(old_ir, new_ir)

        # 判定结论原子归档（按 新版本/rules_version 存放，便于复现）
        report_dir = output_dir / version_key / "checks" / RULES_VERSION
        _atomic_write_tree(
            report_dir,
            {_REPORT_FILE: json.dumps(report.to_dict(), ensure_ascii=False, indent=2)},
        )

        if raise_on_breaking and not report.compatible:
            raise IncompatibleContractError(
                [c.format() for c in report.breaking_changes]
            )
        return report

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

        复现结论不盖时间戳：相同契约 + 相同规则版本 => 完全一致的报告。
        """
        old_ir = parse_contract(old_contract)
        new_ir = parse_contract(new_contract)
        if rules_version is None:
            from .versioning import _RULESETS
            return _RULESETS[RULES_VERSION].check(old_ir, new_ir, stamp_time=False)
        return check_with_rules(old_ir, new_ir, rules_version)


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
