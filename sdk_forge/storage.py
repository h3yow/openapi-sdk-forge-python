"""Atomic artifact storage with per-version generation locks.

Layout under ``base_dir``::

    artifacts/<target>/<version>-<fingerprint12>/   # completed bundles
    reports/<target>.jsonl                          # append-only verdict history
    locks/<target>__<fingerprint>.lock              # held while generating
    staging/<target>__<fingerprint>.tmp.<pid>/      # work-in-progress

Properties:

* Completed artifacts are published atomically (``os.replace`` into place).
* A failed generation removes its own staging directory; stale staging left by
  crashed processes is swept on the next attempt *before* the lock is taken,
  so readers never observe half-written products.
* A second, identical (same fingerprint) trigger while generation is in
  progress raises :class:`GenerationInProgressError`; it never overwrites or
  corrupts a completed result.
* A completed artifact is content-addressed (fingerprint) and therefore never
  overwritten: re-publishing the same bundle just returns the same directory.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
from pathlib import Path

from .compat import CompatibilityReport
from .errors import ArtifactWriteError, GenerationInProgressError
from .generator import GeneratedBundle


def _safe_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return segment or "default"


class ArtifactStore:
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        (self.base_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "staging").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "locks").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "reports").mkdir(parents=True, exist_ok=True)

    # ---- paths ----------------------------------------------------------- #

    def _target_dir(self, target: str) -> Path:
        return self.base_dir / "artifacts" / _safe_segment(target)

    def artifact_dir(self, target: str, version: str, fingerprint: str) -> Path:
        return (
            self._target_dir(target)
            / f"{_safe_segment(version)}-{fingerprint[:12]}"
        )

    def has_artifact(self, target: str, version: str, fingerprint: str) -> bool:
        return self.artifact_dir(target, version, fingerprint).is_dir()

    def _lock_path(self, target: str, fingerprint: str) -> Path:
        return (
            self.base_dir
            / "locks"
            / f"{_safe_segment(target)}__{fingerprint[:12]}.lock"
        )

    def _staging_glob(self, target: str, fingerprint: str):
        return self.base_dir.glob(
                f"staging/{_safe_segment(target)}__{fingerprint[:12]}.tmp.*"
        )

    # ---- locking --------------------------------------------------------- #

    @contextlib.contextmanager
    def generation_lock(self, target: str, fingerprint: str):
        """Serialise identical generations without blocking.

        Uses a non-blocking ``flock`` keyed by (target, fingerprint): a second
        concurrent trigger gets :class:`GenerationInProgressError` immediately,
        while a crashed holder's lock is released by the kernel automatically
        (no permanent stale-lock state).
        """
        lock_path = self._lock_path(target, fingerprint)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Sweep *before* acquiring so a crashed prior run cannot leave residue
        # visible to a new attempt.
        self._sweep_staging(target, fingerprint)
        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GenerationInProgressError(
                    f"generation for target={target!r} fingerprint={fingerprint[:12]!r} "
                    "is already in progress",
                    details={"target": target, "fingerprint": fingerprint[:12]},
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid()}))
            handle.flush()
            yield
        finally:
            self._sweep_staging(target, fingerprint)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _sweep_staging(self, target: str, fingerprint: str) -> None:
        for stale in self._staging_glob(target, fingerprint):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)

    # ---- publishing ------------------------------------------------------ #

    def new_staging_dir(self, target: str, fingerprint: str) -> Path:
        staging = (
            self.base_dir
            / "staging"
            / f"{_safe_segment(target)}__{fingerprint[:12]}.tmp.{os.getpid()}"
        )
        try:
            staging.mkdir(parents=True)
        except OSError as exc:  # pragma: no cover - filesystem failure class
            raise ArtifactWriteError(
                f"cannot create staging directory {staging}: {exc}",
                details={"staging": str(staging)},
            ) from exc
        return staging

    def publish_bundle(
        self, target: str, version: str, fingerprint: str, bundle: GeneratedBundle
    ) -> str:
        """Write the bundle to staging, then atomically publish it.

        On any write error the staging directory is removed and no artifact
        directory is created.  An existing completed artifact is never
        overwritten: its path is returned unchanged.
        """
        final_dir = self.artifact_dir(target, version, fingerprint)
        if final_dir.is_dir():
            return str(final_dir)

        staging = self.new_staging_dir(target, fingerprint)
        try:
            for gen_file in bundle.files:
                dest = staging / gen_file.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                # atomic per-file write: temp sibling then os.replace
                tmp = dest.with_name(dest.name + ".part")
                try:
                    tmp.write_text(gen_file.content, encoding="utf-8")
                    os.replace(tmp, dest)
                except OSError as exc:
                    raise ArtifactWriteError(
                        f"failed writing artifact file {gen_file.path!r}: {exc}",
                        details={"path": gen_file.path},
                    ) from exc
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, final_dir)
            except OSError as exc:
                raise ArtifactWriteError(
                    f"failed publishing artifact directory: {exc}",
                    details={"target": str(final_dir)},
                ) from exc
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return str(final_dir)

    def artifact_path(
        self, target: str, version: str, fingerprint: str, rel: str
    ) -> Path:
        base = self.artifact_dir(target, version, fingerprint).resolve()
        candidate = (base / rel).resolve()
        # path-traversal guard
        if base not in candidate.parents and candidate != base:
            raise FileNotFoundError(rel)
        if not candidate.is_file():
            raise FileNotFoundError(rel)
        return candidate

    # ---- report history -------------------------------------------------- #

    def _report_lock(self, target: str) -> Path:
        return self.base_dir / "locks" / f"report-{_safe_segment(target)}.lock"

    def append_report(self, target: str, report: CompatibilityReport) -> Path:
        """Append a verdict (JSON line) for historical reproducibility.

        Concurrent appenders are serialised with a blocking ``flock`` and each
        report is written in a single ``write`` call, so replay never sees a
        half-written or interleaved line.
        """
        path = self.base_dir / "reports" / f"{_safe_segment(target)}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        lock = self._report_lock(target)
        with open(lock, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        return path

    def list_reports(self, target: str) -> list[dict]:
        path = self.base_dir / "reports" / f"{_safe_segment(target)}.jsonl"
        if not path.is_file():
            return []
        reports: list[dict] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    reports.append(json.loads(line))
        return reports
