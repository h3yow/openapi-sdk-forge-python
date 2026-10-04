"""演示服务：业务接口 + 契约驱动的 SDK 生成/兼容性管理 API。

业务接口（items）用于暴露一份真实的 OpenAPI 契约；
``/forge/*`` 用于触发 SDK 生成、兼容性判定与历史复现。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from sdk_forge.errors import ForgeError
from sdk_forge.service import ForgeService
from sdk_forge.versioning import RULES_VERSION

# ---------------------------------------------------------------------------
# 业务模型与接口（构成被生成的契约）
# ---------------------------------------------------------------------------


class Item(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str] = Field(default_factory=list)


class ItemCreate(BaseModel):
    name: str
    price: float = 0.0
    tags: list[str] = Field(default_factory=list)


class ItemPatch(BaseModel):
    name: str | None = None
    price: float | None = None


class GenerateRequest(BaseModel):
    base_url: str | None = None
    expected_version: str | None = None
    package_name: str = "generated_sdk"
    timeout: float = 5.0
    # 默认只为业务接口生成 SDK；显式传 null 表示纳入全部 tag
    include_tags: list[str] | None = ["items"]


class CheckCompatibilityRequest(BaseModel):
    base_url: str | None = None
    timeout: float = 5.0
    # 缺省按当前规则版本判定；传入未冻结的版本会返回 400 unknown_rules_version
    rules_version: str | None = None


class ReplayRequest(BaseModel):
    old_contract: dict
    new_contract: dict
    rules_version: str | None = None


class JudgeRequest(BaseModel):
    old_contract: dict | None = None
    new_contract: dict
    rules_version: str | None = None
    wait: bool = False


class CompareRulesRequest(BaseModel):
    old_contract: dict | None = None
    new_contract: dict
    rules_versions: list[str] | None = None


def create_app(artifact_root: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="Shop API", version="1.0.0")
    _ITEMS: dict[int, dict] = {
        1: {"id": 1, "name": "widget", "price": 9.5, "tags": ["sale"]}
    }
    _NEXT_ID = 2

    @app.get("/items", tags=["items"], response_model=list[Item])
    def list_items(tag: str | None = None) -> list[dict]:
        if tag is None:
            return list(_ITEMS.values())
        return [i for i in _ITEMS.values() if tag in i["tags"]]

    @app.post("/items", tags=["items"], response_model=Item, status_code=201)
    def create_item(body: ItemCreate) -> dict:
        nonlocal _NEXT_ID
        item = {"id": _NEXT_ID, "name": body.name, "price": body.price,
                "tags": body.tags}
        _ITEMS[_NEXT_ID] = item
        _NEXT_ID += 1
        return item

    @app.get("/items/{item_id}", tags=["items"], response_model=Item)
    def get_item(item_id: int) -> dict:
        if item_id not in _ITEMS:
            raise HTTPException(status_code=404, detail="item not found")
        return _ITEMS[item_id]

    @app.patch("/items/{item_id}", tags=["items"], response_model=Item)
    def patch_item(item_id: int, body: ItemPatch) -> dict:
        if item_id not in _ITEMS:
            raise HTTPException(status_code=404, detail="item not found")
        for key, value in body.model_dump(exclude_none=True).items():
            _ITEMS[item_id][key] = value
        return _ITEMS[item_id]

    @app.delete("/items/{item_id}", tags=["items"], status_code=204)
    def delete_item(item_id: int) -> Response:
        _ITEMS.pop(item_id, None)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Forge 管理 API
    # ------------------------------------------------------------------

    forge = ForgeService()
    artifact_root = Path(artifact_root) if artifact_root is not None else Path(
        __file__
    ).parent / "artifacts"

    def _raise_for_forge_error(exc: ForgeError) -> None:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc),
                    **({"fragment": exc.fragment}  # type: ignore[attr-defined]
                       if hasattr(exc, "fragment") else {}),
                       **({"breaking_changes": exc.breaking_changes}  # type: ignore[attr-defined]
                          if hasattr(exc, "breaking_changes") else {})},
        ) from exc

    @app.post("/forge/generate", tags=["forge"])
    def generate_sdk_endpoint(req: GenerateRequest, request: Request):
        """为服务（默认本服务自身）生成 SDK，返回产物摘要。"""
        try:
            return forge.generate(
                req.base_url or str(request.base_url).rstrip("/"),
                artifact_root,
                expected_version=req.expected_version,
                timeout=req.timeout,
                package_name=req.package_name,
                include_tags=set(req.include_tags) if req.include_tags else None,
            )
        except ForgeError as exc:
            _raise_for_forge_error(exc)

    @app.get("/forge/artifacts/{version}/sdk.zip", tags=["forge"])
    def download_sdk(version: str):
        path = artifact_root / version / "sdk.zip"
        if not path.exists():
            raise HTTPException(status_code=404, detail="artifact not found")
        return Response(
            content=path.read_bytes(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="sdk-{version}.zip"'},
        )

    @app.post("/forge/check-compatibility", tags=["forge"])
    def check_compatibility_endpoint(req: CheckCompatibilityRequest,
                                     request: Request):
        """拉取运行时契约，与已归档的上一版契约做兼容性判定。

        并发触发同一组合时复用同一份结论（内部按 wait 模式去重）；
        ``rules_version`` 未冻结时返回 400 ``unknown_rules_version``。
        """
        try:
            report = forge.check_compatibility(
                req.base_url or str(request.base_url).rstrip("/"),
                artifact_root,
                timeout=req.timeout,
                rules_version=req.rules_version or RULES_VERSION,
            )
            return report.to_dict()
        except ForgeError as exc:
            _raise_for_forge_error(exc)

    @app.post("/forge/replay", tags=["forge"])
    def replay_endpoint(req: ReplayRequest):
        """用提交的两份契约（及规则版本）复现历史判定。"""
        try:
            report = forge.replay(
                old_contract=req.old_contract,
                new_contract=req.new_contract,
                rules_version=req.rules_version,
            )
        except ForgeError as exc:
            _raise_for_forge_error(exc)
        return report.to_dict()

    @app.post("/forge/judge", tags=["forge"])
    def judge_endpoint(req: JudgeRequest):
        """对一对契约执行判定并归档（幂等、并发去重）。

        并发触发同一组合：默认 409 judgement_in_progress；
        ``wait=true`` 时等待并复用正在进行的结论。
        """
        try:
            report = forge.judge(
                old_contract=req.old_contract,
                new_contract=req.new_contract,
                output_dir=artifact_root,
                rules_version=req.rules_version or RULES_VERSION,
                wait=req.wait,
            )
        except ForgeError as exc:
            _raise_for_forge_error(exc)
        return report.to_dict()

    @app.get("/forge/judgements", tags=["forge"])
    def list_judgements_endpoint():
        """列出已归档判定记录（按 旧版本/新版本/规则版本）。"""
        return {"judgements": forge.list_judgements(artifact_root)}

    @app.post("/forge/compare-rules", tags=["forge"])
    def compare_rules_endpoint(req: CompareRulesRequest):
        """同一份契约演进在各冻结规则版本下的结论对比（规则升级影响面评估）。"""
        try:
            compared = forge.compare_rules(
                old_contract=req.old_contract,
                new_contract=req.new_contract,
                versions=req.rules_versions,
            )
        except ForgeError as exc:
            _raise_for_forge_error(exc)
        return {
            version: report.to_dict() for version, report in compared.items()
        }

    return app


app = create_app()
