"""Schemas for template and template column endpoints."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

try:  # pragma: no cover - compatibility with pydantic v1/v2
    from pydantic import ConfigDict
except ImportError:  # pragma: no cover - fallback for pydantic v1
    ConfigDict = None  # type: ignore[misc]

TemplateStatus = Literal["unpublished", "published"]


class TemplateColumnRule(BaseModel):
    id: int
    header_rule: list[str] | str | None = Field(
        default=None,
        alias="header rule",
        serialization_alias="header rule",
    )
    rule: dict[str, Any] | list[Any] | None = None
    summary: Any | None = None
    attachment: str | None = None

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(populate_by_name=True, from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            allow_population_by_field_name = True
            fields = {"header_rule": "header rule"}
            orm_mode = True


class TemplateColumnRuleInput(BaseModel):
    id: int
    header_rule: list[str] | str | None = Field(
        default=None,
        alias="header rule",
        serialization_alias="header rule",
    )

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(populate_by_name=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            allow_population_by_field_name = True
            fields = {"header_rule": "header rule"}


class TemplateColumnBase(BaseModel):
    name: str = Field(..., max_length=50)
    description: str | None = Field(default=None, max_length=255)
    rules: list[TemplateColumnRuleInput] | None = Field(default=None)


class TemplateColumnCreate(TemplateColumnBase):
    """Payload required to create a template column."""


class TemplateColumnBulkCreate(BaseModel):
    """Payload wrapper to create many columns at once."""

    columns: list[TemplateColumnCreate]


class TemplateColumnUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=255)
    rules: list[TemplateColumnRuleInput] | None = Field(default=None)
    is_active: bool | None = None

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(extra="forbid")
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            extra = "forbid"


class TemplateColumnBulkUpdate(BaseModel):
    """Payload wrapper to update many columns at once."""

    columns: list[TemplateColumnUpdate]


class TemplateColumnRead(TemplateColumnBase):
    id: int
    template_id: int
    data_type: str
    rules: list[TemplateColumnRule] | None = Field(default=None)
    created_at: datetime | None
    updated_at: datetime | None
    is_active: bool
    deleted: bool
    deleted_by: int | None
    deleted_at: datetime | None

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            orm_mode = True


class TemplateBase(BaseModel):
    name: str = Field(..., max_length=50)
    table_name: str = Field(..., max_length=63)
    description: str | None = Field(default=None, max_length=255)


class TemplateCreate(TemplateBase):
    """Payload required to create a template."""


class TemplateDuplicate(BaseModel):
    """Payload required to duplicate an existing template."""

    name: str = Field(..., max_length=50)
    table_name: str = Field(..., max_length=63)
    description: str = Field(..., max_length=255)


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=255)
    status: TemplateStatus | None = None
    table_name: str | None = Field(default=None, max_length=63)
    is_active: bool | None = None

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(extra="forbid")
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            extra = "forbid"


class TemplateStatusUpdate(BaseModel):
    status: TemplateStatus

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(extra="forbid")
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            extra = "forbid"


class TemplateSummaryRead(BaseModel):
    id: int
    user_id: int
    name: str
    status: TemplateStatus
    description: str | None
    table_name: str
    created_at: datetime | None
    updated_at: datetime | None
    is_active: bool
    deleted: bool
    deleted_by: int | None
    deleted_at: datetime | None

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            orm_mode = True


class TemplateRead(BaseModel):
    id: int
    user_id: int
    name: str
    status: TemplateStatus
    description: str | None
    table_name: str
    created_at: datetime | None
    updated_at: datetime | None
    is_active: bool
    deleted: bool
    deleted_by: int | None
    deleted_at: datetime | None
    columns: list[TemplateColumnRead]

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            orm_mode = True


class TemplateAssignmentUserRead(BaseModel):
    id: int
    name: str
    email: EmailStr

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            orm_mode = True


class TemplateWithAssignmentsRead(BaseModel):
    template: TemplateRead
    creator: TemplateAssignmentUserRead | None
    assigned_users: list[TemplateAssignmentUserRead]

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            orm_mode = True
