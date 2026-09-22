"""HTTP layer: the demo API service plus the forge management API.

Two separate ASGI applications are composed:

* :func:`build_demo_app` ("items") - the resource service whose OpenAPI
  document is the forge's *input*.  Rich enough to exercise every supported
  feature (path/query params, JSON body, 2xx/4xx responses, nullable fields).

    GET  /items/{item_id}?verbose=   -> ItemModel 200 / ErrorBody 404
    POST /items                      -> ItemModel 201 / ErrorBody 400

* :func:`build_forge_app` - management API mounted under ``/forge`` on the
  combined :data:`app`.  It is intentionally a separate application so its
  untyped management responses never leak into the demo service contract:

    POST /forge/generate
        body: {"base_url": "...", "version"?: "...", "target"?: "..."}
        -> 200 existing artifact (idempotent), 201 newly generated,
           409 generation already in progress / version mismatch,
           422 malformed contract, 502 service unreachable / fetch failed.
    GET  /forge/artifacts/{target}/{version}/{fingerprint}/{file:path}
    POST /forge/compat
        body: {"old_base_url", "new_base_url", "target"?, "rule_version"?}
        -> {"compatible", "rule_version", "changes", fingerprints...}
    GET  /forge/reports/{target}
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import compat as compat_mod
from . import contract as contract_mod
from .errors import ForgeError
from .generator import generate_sdk
from .storage import ArtifactStore

logger = logging.getLogger("sdk_forge.service")


class RootInfo(BaseModel):
    service: str
    version: str


class ItemModel(BaseModel):
    id: int
    name: str
    price: float | None = None
    status: str | None = None
    tags: list[str] = Field(default_factory=list)


class ItemCreate(BaseModel):
    name: str
    price: float | None = None
    status: str | None = "draft"
    tags: list[str] = Field(default_factory=list)


class ErrorBody(BaseModel):
    detail: str


# --------------------------------------------------------------------------- #
# Demo resource service
# --------------------------------------------------------------------------- #

def build_demo_app() -> FastAPI:
    demo = FastAPI(title="Items Demo Service", version="1.0.0")

    items: dict[int, dict[str, Any]] = {
        1: {"id": 1, "name": "hammer", "price": 12.5, "status": "active",
            "tags": ["tool"]},
        2: {"id": 2, "name": "nail", "price": 0.1, "status": "draft",
            "tags": []},
    }
    lock = threading.Lock()

    @demo.get("/", tags=["demo"], response_model=RootInfo)
    def read_root():
        return {"service": "items-demo", "version": demo.version}

    @demo.get("/items/{item_id}", tags=["demo"], response_model=ItemModel,
              responses={404: {"model": ErrorBody}})
    def get_item(item_id: int, verbose: bool = False):
        with lock:
            item = items.get(item_id)
        if item is None:
            return JSONResponse(status_code=404,
                                content={"detail": f"item {item_id} not found"})
        if not verbose:
            item = {k: v for k, v in item.items() if k != "tags"}
        return item

    @demo.post("/items", tags=["demo"], response_model=ItemModel, status_code=201,
               responses={400: {"model": ErrorBody}})
    def create_item(payload: ItemCreate):
        if not payload.name:
            return JSONResponse(status_code=400,
                                content={"detail": "field 'name' is required"})
        if payload.status not in (None, "draft", "active", "archived"):
            return JSONResponse(status_code=400,
                                content={"detail": "invalid status"})
        with lock:
            new_id = max(items, default=0) + 1
            record = {
                "id": new_id,
                "name": payload.name,
                "price": payload.price,
                "status": payload.status or "draft",
                "tags": payload.tags,
            }
            items[new_id] = record
        return JSONResponse(status_code=201, content=record)

    _install_uniform_validation_errors(demo)
    _strip_default_error_schemas(demo)
    return demo


def _install_uniform_validation_errors(app: FastAPI) -> None:
    """Replace FastAPI's default polymorphic 422 body with simple ErrorBody."""
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors()[0]["msg"] if exc.errors() else "invalid request"},
        )


def _strip_default_error_schemas(app: FastAPI) -> None:
    """Remove built-in HTTPValidationError/ValidationError from the contract.

    They use polymorphic ``anyOf`` arrays outside the supported contract
    scope; the uniform 422 handler above replaces them.
    """
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        components.pop(name, None)
    for item in schema.get("paths", {}).values():
        for operation in item.values():
            if isinstance(operation, dict) and "responses" in operation:
                operation["responses"].pop("422", None)
    app.openapi_schema = schema


# --------------------------------------------------------------------------- #
# Forge management API
# --------------------------------------------------------------------------- #

_ERROR_STATUS = {
    "service_unreachable": 502,
    "contract_fetch_failed": 502,
    "malformed_contract": 422,
    "version_mismatch": 409,
    "generation_in_progress": 409,
    "artifact_write_failed": 500,
}


class GenerateRequest(BaseModel):
    base_url: str = Field(min_length=1)
    version: str | None = None
    target: str = "default"


class CompatRequest(BaseModel):
    old_base_url: str = Field(min_length=1)
    new_base_url: str = Field(min_length=1)
    target: str = "default"
    rule_version: str | None = None


def _store() -> ArtifactStore:
    return ArtifactStore(os.environ.get("SDK_FORGE_STORE", "./.forge-store"))


def _forge_error_response(exc: ForgeError) -> JSONResponse:
    logger.warning("forge error code=%s details=%s message=%s",
                   exc.code, exc.details, exc.message)
    return JSONResponse(
        status_code=_ERROR_STATUS.get(exc.code, 500),
        content={"error": exc.code, "message": exc.message, "details": exc.details},
    )


def build_forge_app() -> FastAPI:
    forge = FastAPI(title="SDK Forge Admin", version="0.1.0")

    @forge.post("/generate")
    def generate(req: GenerateRequest):
        store = _store()
        try:
            loaded = contract_mod.load_contract(
                req.base_url, expected_version=req.version
            )
        except ForgeError as exc:
            return _forge_error_response(exc)

        target, fp = req.target, loaded.fingerprint
        if store.has_artifact(target, loaded.version, fp):
            logger.info("artifact already present target=%s version=%s fp=%s",
                        target, loaded.version, fp[:12])
            return {
                "status": "exists", "target": target,
                "version": loaded.version, "fingerprint": fp,
                "artifact_path": str(store.artifact_dir(target, loaded.version, fp)),
            }

        try:
            with store.generation_lock(target, fp):
                if store.has_artifact(target, loaded.version, fp):
                    return {
                        "status": "exists", "target": target,
                        "version": loaded.version, "fingerprint": fp,
                        "artifact_path": str(store.artifact_dir(target, loaded.version, fp)),
                    }
                bundle = generate_sdk(loaded)
                logger.info(
                    "generating SDK target=%s version=%s fp=%s operations=%s",
                    target, loaded.version, fp[:12],
                    [op.operation_id for op in loaded.operations],
                )
                path = store.publish_bundle(target, loaded.version, fp, bundle)
        except ForgeError as exc:
            return _forge_error_response(exc)

        return JSONResponse(status_code=201, content={
            "status": "generated", "target": target,
            "version": loaded.version, "fingerprint": fp,
            "artifact_path": path, "package_name": bundle.package_name,
            "files": [gen_file.path for gen_file in bundle.files],
        })

    @forge.get("/artifacts/{target}/{version}/{fingerprint}/{file_path:path}")
    def get_artifact(target: str, version: str, fingerprint: str, file_path: str):
        try:
            full = _store().artifact_path(target, version, fingerprint, file_path)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="artifact file not found")
        return FileResponse(full)

    @forge.post("/compat")
    def compatibility(req: CompatRequest):
        try:
            old_contract = contract_mod.load_contract(req.old_base_url)
            new_contract = contract_mod.load_contract(req.new_base_url)
        except ForgeError as exc:
            return _forge_error_response(exc)

        rule_version = req.rule_version or compat_mod.RULE_VERSION
        try:
            report = compat_mod.check_backward_compatible(
                old_contract, new_contract, rule_version=rule_version
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "unknown_rule_version", "message": str(exc),
                         "details": {"known": sorted(compat_mod.RULE_SETS)}},
            )
        _store().append_report(req.target, report)
        return report.to_dict()

    @forge.get("/reports/{target}")
    def reports(target: str):
        return {"target": target, "reports": _store().list_reports(target)}

    return forge


def build_app() -> FastAPI:
    """Combined application: demo service at /, forge admin mounted at /forge."""
    app = build_demo_app()
    app.mount("/forge", build_forge_app())
    return app


app = build_app()
