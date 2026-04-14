"""Schemas for validation rule endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

try:  # Pydantic v2
    from pydantic import ConfigDict
except ImportError:  # pragma: no cover - compatibility path for pydantic v1
    ConfigDict = None  # type: ignore[misc]

JSONType = dict[str, Any] | list[Any]
JSONObject = dict[str, Any]


class RuleBase(BaseModel):
    rule: JSONType


class RuleCreate(RuleBase):
    """Payload required to create a rule."""

    is_active: bool = True


class RuleUpdate(BaseModel):
    rule: JSONType | None = None
    is_active: bool | None = None

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(extra="forbid")
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            extra = "forbid"


class RuleRead(RuleBase):
    id: int
    summary: JSONObject | None
    attachment: str | None
    created_by: int | None
    created_at: datetime | None
    updated_by: int | None
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


class RuleByType(BaseModel):
    id: int
    nombre_de_la_regla: str = Field(..., alias="Nombre de la regla")
    tipo_de_dato: str = Field(..., alias="Tipo de dato")
    campo_obligatorio: bool = Field(..., alias="Campo obligatorio")
    mensaje_de_error: str = Field(..., alias="Mensaje de error")
    descripcion: str = Field(..., alias="Descripción")
    ejemplo: Any = Field(..., alias="Ejemplo")
    header: list[str] = Field(default_factory=list, alias="Header")
    header_rule: list[str] = Field(default_factory=list, alias="Header rule")
    regla: JSONType = Field(..., alias="Regla")

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(populate_by_name=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            allow_population_by_field_name = True


class RuleHeaderResponse(BaseModel):
    id: int
    tipo_de_dato: str = Field(..., alias="Tipo de dato")
    headers: list[str] = Field(default_factory=list, alias="Header")
    header_rule: list[str] = Field(default_factory=list, alias="Header rule")

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(populate_by_name=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            allow_population_by_field_name = True
