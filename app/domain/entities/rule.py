"""Domain entity representing a validation rule."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Rule:
    """Core attributes describing a validation rule."""

    id: int | None
    rule: dict[str, Any] | list[Any]
    summary: str | None
    attachment: str | None
    created_by: int | None
    created_at: datetime | None
    updated_by: int | None
    updated_at: datetime | None
    is_active: bool
    deleted: bool
    deleted_by: int | None
    deleted_at: datetime | None


__all__ = ["Rule"]
