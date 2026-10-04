"""测试全局配置：确保单测日志打印契约输入与判定依据（配合 pytest -s）。

同时在日志里给出「覆盖了哪些业务（测试文件）、各业务用了哪些用例、
每个用例的通过情况」，便于回归时直接核对覆盖面。
"""

import logging
import os
import sys
from collections import defaultdict

# 仅在直接运行 pytest（有 -s 输出通道）时配置流日志，避免被插件捕获后重复
logging.basicConfig(
    level=os.environ.get("TEST_LOG_LEVEL", "INFO"),
    stream=sys.stdout,
    format="%(asctime)s [%(name)s] %(message)s",
)

logger = logging.getLogger("pytest-coverage-map")


def pytest_collection_finish(session):
    """收集结束：列出每个业务（测试文件）覆盖了哪些用例。"""
    by_file: dict[str, list[str]] = defaultdict(list)
    for item in session.items:
        by_file[item.path.name].append(item.name)
    lines = ["测试覆盖总览（业务 -> 用例）:"]
    for fname in sorted(by_file):
        lines.append(f"  {fname} ({len(by_file[fname])} 用例):")
        for name in sorted(by_file[fname]):
            lines.append(f"    - {name}")
    logger.info("\n".join(lines))


def pytest_runtest_logreport(report):
    """每个用例执行结束：记录通过情况。"""
    if report.when == "call":
        logger.info("用例结果 %s -> %s", report.nodeid, report.outcome.upper())

