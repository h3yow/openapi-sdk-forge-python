"""Artifact storage tests: atomic publish, duplicate triggers, no residue."""

from __future__ import annotations

import json
import threading

import pytest

from sdk_forge.compat import (
    Change,
    CompatibilityReport,
    RULE_VERSION,
    Severity,
)
from sdk_forge.errors import GenerationInProgressError
from sdk_forge.generator import GeneratedBundle, GeneratedFile
from sdk_forge.storage import ArtifactStore


def _bundle(fingerprint: str = "a" * 64) -> GeneratedBundle:
    return GeneratedBundle(
        files=[
            GeneratedFile("__init__.py", "INIT\n"),
            GeneratedFile("client.py", "CLIENT\n"),
            GeneratedFile("codegen.json", json.dumps({"f": fingerprint}) + "\n"),
        ],
        package_name="demo_sdk",
        contract_title="Demo",
        contract_version="1.0.0",
        fingerprint=fingerprint,
    )


def _report(compatible: bool) -> CompatibilityReport:
    return CompatibilityReport(
        compatible=compatible,
        rule_version=RULE_VERSION,
        old_fingerprint="o" * 64,
        new_fingerprint="n" * 64,
        changes=[
            Change("R002-field-removed", Severity.BREAKING,
                   "operation:x / field:y", "removed"),
        ]
        if not compatible
        else [],
    )


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(str(tmp_path / "artifacts-root"))


def test_publish_creates_content_addressed_dir(store):
    path = store.publish_bundle("demo", "1.0.0", "a" * 64, _bundle())
    assert store.has_artifact("demo", "1.0.0", "a" * 64)
    assert path.endswith(f"1.0.0-{'a' * 12}")
    located = store.artifact_path("demo", "1.0.0", "a" * 64, "client.py")
    assert located.read_text() == "CLIENT\n"


def test_publish_is_idempotent_and_never_overwrites(store):
    b1 = _bundle("a" * 64)
    first = store.publish_bundle("demo", "1.0.0", "a" * 64, b1)
    # same fingerprint, even "different" content: completed result is kept
    b2 = GeneratedBundle(
        files=[GeneratedFile("__init__.py", "DIFFERENT\n")],
        package_name="demo_sdk",
        contract_title="Demo",
        contract_version="1.0.0",
        fingerprint="a" * 64,
    )
    second = store.publish_bundle("demo", "1.0.0", "a" * 64, b2)
    assert first == second
    assert store.artifact_path("demo", "1.0.0", "a" * 64, "__init__.py").read_text() == "INIT\n"


def test_duplicate_concurrent_trigger_is_rejected_not_queued(store):
    fp = "b" * 64
    entered = threading.Event()
    proceed = threading.Event()
    result: dict = {}

    def holder():
        with store.generation_lock("demo", fp):
            entered.set()
            proceed.wait(timeout=5)

    def duplicate():
        entered.wait(timeout=5)
        try:
            with store.generation_lock("demo", fp):
                result["entered_duplicate"] = True
        except GenerationInProgressError as exc:
            result["code"] = exc.code

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=duplicate)
    t1.start()
    t2.start()
    t2.join(timeout=5)
    proceed.set()
    t1.join(timeout=5)

    assert result.get("code") == "generation_in_progress"
    assert "entered_duplicate" not in result


def test_different_fingerprints_do_not_block_each_other(store):
    with store.generation_lock("demo", "a" * 64):
        # different fingerprint: lock must be acquirable
        with store.generation_lock("demo", "c" * 64):
            assert True


def test_failed_publish_leaves_no_artifact_or_staging(store):
    class BadBundle(GeneratedBundle):
        pass

    bad = GeneratedBundle(
        files=[GeneratedFile("__init__.py", "ok\n"),
               GeneratedFile("sub/deep/file.py", "x\n")],
        package_name="demo_sdk",
        contract_title="Demo",
        contract_version="2.0.0",
        fingerprint="f" * 64,
    )

    real_write_text = None
    import pathlib

    original = pathlib.Path.write_text

    def failing_write(self, data, *a, **k):
        if self.name.startswith("file.py"):
            raise OSError("disk full")
        return original(self, data, *a, **k)

    pathlib.Path.write_text = failing_write
    try:
        with pytest.raises(Exception):
            store.publish_bundle("demo", "2.0.0", "f" * 64, bad)
    finally:
        pathlib.Path.write_text = original

    assert not store.has_artifact("demo", "2.0.0", "f" * 64)
    staging_left = list((store.base_dir / "staging").iterdir())
    assert staging_left == []


def test_lock_release_allows_retry_after_failure(store):
    fp = "d" * 64
    with pytest.raises(RuntimeError):
        with store.generation_lock("demo", fp):
            raise RuntimeError("boom")
    # lock must be released; a new identical attempt is admitted
    with store.generation_lock("demo", fp):
        assert True


def test_reports_are_appended_and_replayed(store):
    store.append_report("demo", _report(False))
    store.append_report("demo", _report(True))
    reports = store.list_reports("demo")
    assert [r["compatible"] for r in reports] == [False, True]
    assert reports[0]["rule_version"] == RULE_VERSION
    assert reports[0]["changes"][0]["rule_id"] == "R002-field-removed"


def test_concurrent_report_appends_never_interleave(store):
    n = 20
    errors: list[Exception] = []

    def append(i):
        try:
            store.append_report("demo", _report(i % 2 == 0))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    reports = store.list_reports("demo")
    assert len(reports) == n
    # every line is a complete JSON object with the expected schema
    for report in reports:
        assert set(report) == {
            "compatible", "rule_version", "old_fingerprint",
            "new_fingerprint", "changes",
        }


def test_artifact_path_blocks_traversal(store):
    store.publish_bundle("demo", "1.0.0", "a" * 64, _bundle())
    with pytest.raises(FileNotFoundError):
        store.artifact_path("demo", "1.0.0", "a" * 64, "../../../etc/passwd")
