"""测试全局配置：确保单测日志打印契约输入与判定依据（配合 pytest -s）。"""

import logging
import os
import sys

# 仅在直接运行 pytest（有 -s 输出通道）时配置流日志，避免被插件捕获后重复
logging.basicConfig(
    level=os.environ.get("TEST_LOG_LEVEL", "INFO"),
    stream=sys.stdout,
    format="%(asctime)s [%(name)s] %(message)s",
)
