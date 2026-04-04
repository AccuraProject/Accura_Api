"""Use case for changing the authenticated user's password."""

from dataclasses import replace

from sqlalchemy.orm import Session

from app.domain.entities import User
from app.infrastructure.repositories import UserRepository
from app.infrastructure.security import get_password_hash, verify_password
from app.utils import now_in_app_timezone


def change_password(
    session: Session,
    *,
    user_id: int,
    current_password: str,
    new_password: str,
) -> User:
    """Change the password of the authenticated user."""

    repository = UserRepository(session)
    user = repository.get(user_id)
    if user is None:
        raise ValueError("Usuario no encontrado")
    if not user.is_active:
        raise ValueError("Usuario inactivo")
    if not verify_password(current_password, user.password):
        raise ValueError("La contrasena actual es incorrecta")
    if verify_password(new_password, user.password):
        raise ValueError("La nueva contrasena no puede ser igual a la actual")

    updated_user = replace(
        user,
        password=get_password_hash(new_password),
        must_change_password=False,
        updated_by=user.id,
        updated_at=now_in_app_timezone(),
    )
    return repository.update(updated_user)


__all__ = ["change_password"]
