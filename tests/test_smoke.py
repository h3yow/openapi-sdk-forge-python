"""骨架冒烟测试：确认包可导入。"""

import sdk_forge


def test_package_imports():
    assert sdk_forge.ForgeError is not None
