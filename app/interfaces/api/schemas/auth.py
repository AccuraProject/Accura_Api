"""Authentication related schemas."""

from pydantic import BaseModel, EmailStr, Field


class Token(BaseModel):
    access_token: str
    token_type: str
    role: str
    must_change_password: bool
    impersonation: bool = False
    impersonated_by_user_id: int | None = None
    impersonated_user_id: int | None = None
    expires_in_seconds: int | None = None


class TokenValidationResponse(BaseModel):
    is_valid: bool = Field(
        ..., description="Indica si el token proporcionado es válido y pertenece a un usuario"
    )


class PasswordHashRequest(BaseModel):
    password: str


class PasswordHashResponse(BaseModel):
    hashed_password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(..., description="Correo electrónico registrado del usuario")


class ForgotPasswordResponse(BaseModel):
    message: str
