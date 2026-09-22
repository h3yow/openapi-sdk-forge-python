"""由 OpenAPI 契约自动生成的请求/响应模型，请勿手改。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


@dataclass
class HTTPValidationError:
    detail: list["ValidationError"] = None


@dataclass
class Item:
    id: int
    name: str
    price: float
    tags: list[str] = None


@dataclass
class ItemCreate:
    name: str
    price: float = None
    tags: list[str] = None


@dataclass
class ItemPatch:
    name: str | None = None
    price: float | None = None


@dataclass
class ValidationError:
    loc: list[str | int]
    msg: str
    type: str
    input: object = None
    ctx: dict[str, object] = None
