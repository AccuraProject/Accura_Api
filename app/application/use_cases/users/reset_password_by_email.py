"""Use case to reset a user's password identified by email."""

from dataclasses import replace

from sqlalchemy.orm import Session

from app.domain.entities import User
from app.infrastructure.repositories import UserRepository
from app.infrastructure.security import get_password_hash, generate_secure_password
from app.utils import now_in_app_timezone

from .validators import ensure_valid_email


def reset_password_by_email(session: Session, *, email: str) -> tuple[User, str]:
    """Reset the password of an active user identified by ``email``.

    The function normalizes and validates the email, generates a new secure
    password, updates the stored hash and marks the account so the user must
    change the password on next login.
    """

    normalized_email = ensure_valid_email(email)
    repository = UserRepository(session)

    user = repository.get_by_email(normalized_email)
    if user is None:
        raise ValueError("El correo electrónico no está registrado en el sistema")
    if not user.is_active:
        raise ValueError("El correo electrónico pertenece a un usuario inactivo")

    temporary_password = generate_secure_password()
    hashed_password = get_password_hash(temporary_password)

    updated_user = replace(
        user,
        password=hashed_password,
        must_change_password=True,
        updated_by=user.id,
        updated_at=now_in_app_timezone(),
    )

    persisted_user = repository.update(updated_user)
    return persisted_user, temporary_password


__all__ = ["reset_password_by_email"]
