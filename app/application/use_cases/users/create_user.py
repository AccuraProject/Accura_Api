"""Use case for creating users."""

from sqlalchemy.orm import Session

from app.domain.entities import User
from app.infrastructure.repositories import RoleRepository, UserRepository
from app.infrastructure.security import get_password_hash
from app.utils import now_in_app_timezone
from .validators import ensure_valid_email


def create_user(
    session: Session,
    *,
    name: str,
    role_id: int,
    email: str,
    password: str,
    created_by: int | None = None,
) -> User:
    """Create a new user ensuring unique email addresses."""

    repository = UserRepository(session)
    role_repository = RoleRepository(session)

    normalized_email = ensure_valid_email(email)

    existing_user = repository.get_by_email(normalized_email)
    if existing_user:
        if not existing_user.is_active:
            raise ValueError(
                "El correo electrónico pertenece a un usuario inactivo"
            )
        raise ValueError("El correo electrónico ya está registrado")

    role = role_repository.get(role_id)
    if role is None:
        raise ValueError("Rol no encontrado")

    role_alias = role.alias.lower()
    allowed_roles = role_repository.list_aliases()
    if role_alias not in allowed_roles:
        raise ValueError("Rol no permitido")

    if role_alias == "admin":
        existing_admins = repository.list_ids_by_role_alias("admin")
        if existing_admins:
            raise ValueError(
                "Solo puede existir un usuario administrador en el sistema"
            )

    hashed_password = get_password_hash(password)
    now = now_in_app_timezone()

    must_change_password = role_alias in allowed_roles

    user = User(
        id=None,
        role=role,
        name=name,
        email=normalized_email,
        password=hashed_password,
        must_change_password=must_change_password,
        last_login=None,
        created_by=created_by,
        created_at=now,
        updated_by=None,
        updated_at=None,
        is_active=True,
        deleted=False,
        deleted_by=None,
        deleted_at=None,
    )

    return repository.create(user)
