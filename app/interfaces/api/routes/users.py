"""Rutas para administrar usuarios y sus credenciales."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.application.use_cases.users import (
    create_user as create_user_uc,
    delete_user as delete_user_uc,
    get_user as get_user_uc,
    list_users as list_users_uc,
    update_user as update_user_uc,
)
from app.domain.entities import User
from app.infrastructure.database import get_db
from app.infrastructure.email import (
    send_new_user_credentials_email,
    send_user_credentials_update_email,
    send_user_password_reset_email,
)
from app.interfaces.api.dependencies import get_current_active_user, require_admin
from app.interfaces.api.schemas import UserCreate, UserRead, UserUpdate
from app.infrastructure.security import generate_secure_password
from app.interfaces.api.routes_helpers import compute_credentials_notification

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)


def _to_read_model(user: User) -> UserRead:
    if hasattr(UserRead, "model_validate"):
        return UserRead.model_validate(user)
    return UserRead.from_orm(user)
@router.post("/", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register_user(
    user_in: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Crea un nuevo usuario con credenciales de acceso a la API."""

    generated_password = generate_secure_password()
    try:
        user = create_user_uc(
            db,
            name=user_in.name,
            role_id=user_in.role_id,
            email=user_in.email,
            password=generated_password,
            created_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not send_new_user_credentials_email(user.email, generated_password):
        logger.warning("No se pudo enviar el correo de credenciales al usuario %s", user.email)

    return _to_read_model(user)


@router.get("/me", response_model=UserRead)
def read_current_user(current_user: User = Depends(get_current_active_user)):
    """Devuelve la información del usuario autenticado."""

    return _to_read_model(current_user)


@router.get("/", response_model=list[UserRead])
def list_users(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Devuelve una lista de usuarios registrados."""

    users = list_users_uc(
        db,
        current_user=current_user,
        skip=skip,
        limit=limit,
    )
    return [_to_read_model(user) for user in users]


@router.get("/{user_id}", response_model=UserRead)
def read_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Obtiene al usuario identificado por ``user_id``."""

    try:
        user = get_user_uc(db, user_id, include_inactive=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _to_read_model(user)


@router.put("/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    user_in: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Actualiza los datos de un usuario existente."""

    try:
        target_user = get_user_uc(db, user_id, include_inactive=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if hasattr(user_in, "model_dump"):
        update_data = user_in.model_dump(exclude_unset=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        update_data = user_in.dict(exclude_unset=True)

    is_admin = current_user.is_admin()
    acting_on_self = user_id == current_user.id
    if "must_change_password" in update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede modificar esta configuración manualmente",
        )
    if is_admin and not acting_on_self and "password" in update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Los administradores deben restablecer la contraseña de otros "
                "usuarios desde la opción de restablecimiento"
            ),
        )
    if not is_admin:
        if user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No autorizado",
            )
        if "email" in update_data:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="El cliente no puede cambiar su correo electrónico",
            )
        if "role_id" in update_data:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="El cliente no puede cambiar su rol",
            )
        if "is_active" in update_data:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="El cliente no puede cambiar su estado",
            )
        provided_password = bool(update_data.get("password"))
        allowed_without_password = {"name"}
        non_password_fields = {
            field
            for field in update_data
            if field != "password" and update_data[field] is not None
        }

        name_only_update = (
            non_password_fields
            and non_password_fields <= allowed_without_password
            and "name" in non_password_fields
            and update_data.get("name") is not None
        )

        if not provided_password and not name_only_update:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El cliente debe proporcionar su contraseña para actualizar sus datos",
            )

    email = update_data.get("email")
    email_changed = email is not None and email != target_user.email
    if email is not None and not email_changed:
        email = None
    must_change_password: bool | None = None
    role_id = update_data.get("role_id") if "role_id" in update_data else None
    requested_password = update_data.get("password")

    password = requested_password
    password_for_notification = requested_password
    password_changed = requested_password is not None

    if email_changed:
        if requested_password is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se puede cambiar la contraseña al actualizar el correo electrónico",
            )
        generated_password = generate_secure_password()
        password = generated_password
        password_for_notification = generated_password
        password_changed = True
        must_change_password = True
    elif requested_password is not None:
        must_change_password = False

    update_payload = {
        "user_id": user_id,
        "updated_by": current_user.id,
    }
    if "name" in update_data:
        update_payload["name"] = update_data["name"]
    if email is not None:
        update_payload["email"] = email
    if must_change_password is not None:
        update_payload["must_change_password"] = must_change_password
    if "is_active" in update_data:
        update_payload["is_active"] = update_data["is_active"]
    if role_id is not None:
        update_payload["role_id"] = role_id
    if password is not None:
        update_payload["password"] = password

    try:
        user = update_user_uc(
            db,
            **update_payload,
        )
    except ValueError as exc:
        status_code = status.HTTP_400_BAD_REQUEST
        if str(exc) == "Usuario no encontrado":
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    notification_decision = compute_credentials_notification(
        email_changed=email_changed,
        password_changed=password_changed,
        is_admin=is_admin,
        acting_on_self=acting_on_self,
    )
    if not user.is_active:
        notification_decision = type(notification_decision)(
            should_send=False,
            include_password=False,
        )
    if notification_decision.should_send:
        password_for_email = (
            password_for_notification if notification_decision.include_password else None
        )
        if not send_user_credentials_update_email(
            user.email,
            password_for_email,
            email_changed=email_changed,
            password_changed=password_changed,
        ):
            logger.warning(
                "No se pudo enviar el correo de actualización de credenciales al usuario %s",
                user.email,
            )
    return _to_read_model(user)


@router.post("/{user_id}/reset-password", response_model=UserRead)
def reset_user_password(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Genera una nueva contraseña temporal para el usuario seleccionado."""

    try:
        get_user_uc(db, user_id, include_inactive=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    new_password = generate_secure_password()

    try:
        user = update_user_uc(
            db,
            user_id=user_id,
            password=new_password,
            must_change_password=True,
            updated_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not send_user_password_reset_email(user.email, new_password):
        logger.warning(
            "No se pudo enviar el correo de restablecimiento de contraseña al usuario %s",
            user.email,
        )

    return _to_read_model(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Elimina al usuario indicado."""

    try:
        delete_user_uc(db, user_id, deleted_by=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
