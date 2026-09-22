"""保证提交的 generated_demo 样例与当前服务契约的生成结果一致。"""

import pathlib

import main as main_mod
from sdk_forge.contract import parse_contract
from sdk_forge.generator import generate_sdk

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_demo_sdk_matches_current_contract():
    spec = main_mod.app.openapi()
    ir = parse_contract(spec, include_tags={"items"})
    fresh = generate_sdk(ir, package_name="shop_sdk")
    demo_dir = ROOT / "generated_demo" / "shop_sdk"
    for rel, content in fresh.items():
        disk = demo_dir / rel.removeprefix("shop_sdk/")
        assert disk.exists(), f"演示产物缺少 {disk.name}"
        assert disk.read_text() == content, f"演示产物过期: {disk}"
