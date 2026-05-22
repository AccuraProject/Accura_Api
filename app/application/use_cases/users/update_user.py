"""Use case for updating user information."""

from dataclasses import replace

from sqlalchemy.orm import Session

from app.domain.entities import User
from app.infrastructure.repositories import RoleRepository, UserRepository
from app.infrastructure.security import get_password_hash
from app.utils import now_in_app_timezone
from .validators import ensure_valid_email


def update_user(
    session: Session,
    *,
    user_id: int,
    name: str | None = None,
    email: str | None = None,
    must_change_password: bool | None = None,
    send_emails: bool | None = None,
    is_active: bool | None = None,
    password: str | None = None,
    role_id: int | None = None,
    updated_by: int | None = None,
) -> User:
    """Update the provided user with the new values."""

    repository = UserRepository(session)
    role_repository = RoleRepository(session)
    current_user = repository.get(user_id)
    if current_user is None:
        raise ValueError("Usuario no encontrado")

    new_email = current_user.email
    if email is not None and email != current_user.email:
        normalized_email = ensure_valid_email(email)
        existing_with_email = repository.get_by_email(normalized_email)
        if existing_with_email and existing_with_email.id != user_id:
            raise ValueError("El correo electrónico ya está registrado")
        new_email = normalized_email

    allowed_roles = role_repository.list_aliases()

    new_role = current_user.role
    if role_id is not None and role_id != current_user.role.id:
        role = role_repository.get(role_id)
        if role is None:
            raise ValueError("Rol no encontrado")
        if role.alias.lower() not in allowed_roles:
            raise ValueError("Rol no permitido")
        new_role = role

    if current_user.role.alias.lower() not in allowed_roles:
        raise ValueError("Rol no permitido")

    updated_user = replace(
        current_user,
        role=new_role,
        name=name if name is not None else current_user.name,
        email=new_email,
        must_change_password=(
            must_change_password
            if must_change_password is not None
            else current_user.must_change_password
        ),
        send_emails=send_emails if send_emails is not None else current_user.send_emails,
        is_active=is_active if is_active is not None else current_user.is_active,
        updated_by=updated_by if updated_by is not None else current_user.updated_by,
        updated_at=now_in_app_timezone(),
        deleted=current_user.deleted,
        deleted_by=current_user.deleted_by,
        deleted_at=current_user.deleted_at,
    )

    if password:
        hashed_password = get_password_hash(password)
        updated_user = replace(updated_user, password=hashed_password)

    return repository.update(updated_user)

