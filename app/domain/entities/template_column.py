"""Domain entity representing a template column."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class TemplateColumnRule:
    """Association between a column and a rule."""

    id: int
    headers: tuple[str, ...] | None = None
    group: int | None = None

    def normalized_headers(self) -> tuple[str, ...] | None:
        """Return headers ensuring empty collections become ``None``."""

        if not self.headers:
            return None
        return tuple(header for header in self.headers if header)


@dataclass
class TemplateColumn:
    """Core attributes describing a template column definition."""

    id: int | None
    template_id: int
    rules: tuple[TemplateColumnRule, ...]
    name: str
    description: str | None
    data_type: str
    created_by: int | None
    created_at: datetime | None
    updated_by: int | None
    updated_at: datetime | None
    is_active: bool
    deleted: bool = False
    deleted_by: int | None = None
    deleted_at: datetime | None = None

    @property
    def rule_ids(self) -> tuple[int, ...]:
        """Return the identifiers of all linked rules."""

        return tuple(rule.id for rule in self.rules)

    @property
    def rule_header(self) -> tuple[str, ...] | None:
        """Aggregate all header values associated with the column rules."""

        headers: list[str] = []
        for rule in self.rules:
            if not rule.headers:
                continue
            headers.extend(rule.headers)
        return tuple(headers) if headers else None

    def replace_rules(self, rules: Iterable[TemplateColumnRule]) -> "TemplateColumn":
        """Return a copy of the column with the provided rules."""

        return TemplateColumn(
            id=self.id,
            template_id=self.template_id,
            rules=tuple(rules),
            name=self.name,
            description=self.description,
            data_type=self.data_type,
            created_by=self.created_by,
            created_at=self.created_at,
            updated_by=self.updated_by,
            updated_at=self.updated_at,
            is_active=self.is_active,
            deleted=self.deleted,
            deleted_by=self.deleted_by,
            deleted_at=self.deleted_at,
        )


__all__ = ["TemplateColumn", "TemplateColumnRule"]
