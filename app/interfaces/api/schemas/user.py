"""User schemas."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.infrastructure.security import validate_password_strength

try:  # Pydantic v2
    from pydantic import ConfigDict, field_validator
except ImportError:  # pragma: no cover - compatibility path for pydantic v1
    ConfigDict = None  # type: ignore[misc]
    field_validator = None  # type: ignore[assignment]
    from pydantic import validator


class RoleRead(BaseModel):
    id: int
    name: str
    alias: str

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            orm_mode = True


class UserBase(BaseModel):
    name: str = Field(..., max_length=50)
    email: EmailStr


class UserCreate(UserBase):
    role_id: int = Field(..., ge=1)
    send_emails: bool = True


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8)
    send_emails: bool | None = None
    is_active: bool | None = None
    role_id: int | None = None

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(extra="forbid")
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            extra = "forbid"


class UserRead(BaseModel):
    id: int
    name: str
    email: EmailStr
    send_emails: bool
    must_change_password: bool
    last_login: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    is_active: bool
    deleted: bool
    deleted_by: int | None
    deleted_at: datetime | None
    role: RoleRead

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            orm_mode = True


class UserSummaryRead(BaseModel):
    id: int
    name: str
    email: EmailStr

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            orm_mode = True

class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)

    if ConfigDict is not None:  # pragma: no branch - runtime configuration
        model_config = ConfigDict(extra="forbid")
    else:  # pragma: no cover - compatibility path for pydantic v1
        class Config:
            extra = "forbid"

    if field_validator is not None:  # pragma: no branch

        @field_validator("new_password")  # type: ignore[misc[arg-type]]
        @classmethod
        def validate_new_password(cls, value: str) -> str:
            return validate_password_strength(value, field_name="La nueva contrasena")

    else:  # pragma: no cover - compatibility path for pydantic v1

        @validator("new_password")
        def validate_new_password_v1(cls, value: str) -> str:  # type: ignore[override]
            return validate_password_strength(value, field_name="La nueva contrasena")


class PasswordChangeResponse(BaseModel):
    message: str
