"""Endpoints relacionados con autenticacion y gestion de contrasenas."""

import logging
from datetime import timedelta
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.application.use_cases.users import (
    AuthenticationStatus,
    authenticate_user,
    get_user as get_user_uc,
    record_login,
    reset_password_by_email,
)
from app.config import get_settings
from app.domain.entities import User
from app.infrastructure.database import get_db
from app.infrastructure.email import send_user_password_reset_email
from app.infrastructure.security import create_access_token, get_password_hash
from app.interfaces.api.dependencies import get_current_user, oauth2_scheme, require_admin
from app.interfaces.api.routes_helpers import resolve_password_reset_recipient
from app.interfaces.api.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    PasswordHashRequest,
    PasswordHashResponse,
    Token,
    TokenValidationResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()
logger = logging.getLogger(__name__)
_IMPERSONATION_TOKEN_EXPIRE_SECONDS = 60 * 60

_PASSWORD_RESET_MESSAGE = "Se envio una contrasena temporal al correo registrado."


def _get_creator_user(db: Session, target_user: User) -> User | None:
    if target_user.created_by is None:
        return None
    try:
        return get_user_uc(db, target_user.created_by, include_inactive=True)
    except ValueError:
        return None


# Nota: se conserva la firma esperada por OAuth2PasswordRequestForm.
@router.post("/token", response_model=Token)
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """Autentica al usuario por correo electronico y devuelve un token JWT."""

    user, auth_status = authenticate_user(db, form_data.username, form_data.password)

    if auth_status is AuthenticationStatus.INVALID_CREDENTIALS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if auth_status is AuthenticationStatus.INACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario inactivo",
            headers={"WWW-Authenticate": "Bearer"},
        )

    must_change_password = (
        auth_status is AuthenticationStatus.MUST_CHANGE_PASSWORD or user.must_change_password
    )

    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    password_signature = sha256(
        f"{user.password}:{int(user.is_active)}".encode()
    ).hexdigest()
    access_token = create_access_token(
        data={
            "sub": user.email,
            "role": user.role.alias,
            "pwd_sig": password_signature,
        },
        expires_delta=access_token_expires,
    )
    record_login(db, user.id)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user.role.alias,
        "must_change_password": must_change_password,
    }


@router.post("/impersonate/{user_id}", response_model=Token)
def impersonate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Genera un token temporal para actuar como otro usuario."""

    try:
        target_user = get_user_uc(db, user_id, include_inactive=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not target_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede impersonar un usuario inactivo",
        )

    access_token_expires = timedelta(seconds=_IMPERSONATION_TOKEN_EXPIRE_SECONDS)
    password_signature = sha256(
        f"{target_user.password}:{int(target_user.is_active)}".encode()
    ).hexdigest()
    access_token = create_access_token(
        data={
            "sub": target_user.email,
            "role": target_user.role.alias,
            "pwd_sig": password_signature,
            "impersonation": True,
            "impersonated_by_user_id": current_user.id,
            "impersonated_user_id": target_user.id,
        },
        expires_delta=access_token_expires,
    )

    logger.info(
        "Impersonation started by admin_id=%s admin_email=%s target_user_id=%s target_email=%s",
        current_user.id,
        current_user.email,
        target_user.id,
        target_user.email,
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": target_user.role.alias,
        "must_change_password": False,
        "impersonation": True,
        "impersonated_by_user_id": current_user.id,
        "impersonated_user_id": target_user.id,
        "expires_in_seconds": _IMPERSONATION_TOKEN_EXPIRE_SECONDS,
    }


@router.post("/hash-password", response_model=PasswordHashResponse)
def generate_password_hash(
    payload: PasswordHashRequest,
    _: User = Depends(require_admin),
) -> PasswordHashResponse:
    """Devuelve el hash de la contrasena indicada para uso administrativo."""

    hashed_password = get_password_hash(payload.password)
    return PasswordHashResponse(hashed_password=hashed_password)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def forgot_password(
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
) -> ForgotPasswordResponse:
    """Genera una contrasena temporal y la envia al correo del usuario."""

    try:
        user, temporary_password = reset_password_by_email(
            db,
            email=payload.email,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {
            "El correo electronico debe tener un formato valido",
            "El correo electrónico debe tener un formato válido",
            "El correo electronico no esta registrado en el sistema",
            "El correo electrónico no está registrado en el sistema",
            "El correo electronico pertenece a un usuario inactivo",
            "El correo electrónico pertenece a un usuario inactivo",
        }:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No fue posible procesar la solicitud de restablecimiento",
        ) from exc

    creator_user = _get_creator_user(db, user)
    reset_recipient = resolve_password_reset_recipient(user, creator_user)
    if reset_recipient.email is None:
        logger.warning(
            "No se envio el correo de restablecimiento para user_id=%s porque send_emails esta desactivado y no se encontro el creador",
            user.id,
        )
    elif not send_user_password_reset_email(
        user.email,
        temporary_password,
        recipient=reset_recipient.email,
    ):
        logger.warning(
            "No se pudo enviar el correo de restablecimiento de contrasena para el usuario %s al destinatario %s",
            user.email,
            reset_recipient.email,
        )

    return ForgotPasswordResponse(message=_PASSWORD_RESET_MESSAGE)


@router.get("/validate-token", response_model=TokenValidationResponse)
def validate_token(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> TokenValidationResponse:
    """Valida el token proporcionado y devuelve un indicador booleano."""

    try:
        get_current_user(token=token, db=db)
    except HTTPException:
        return TokenValidationResponse(is_valid=False)

    return TokenValidationResponse(is_valid=True)
