"""ASGI entry point: the demo resource service plus the forge management API.

Run locally with::

    uv run uvicorn main:app --reload
"""

from sdk_forge.service import app

__all__ = ["app"]
