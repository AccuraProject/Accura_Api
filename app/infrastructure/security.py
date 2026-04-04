"""Security helpers for hashing and token generation."""

from datetime import timedelta
import secrets
import string

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings
from app.utils import now_in_app_timezone

# ---- Hashing con UNA SOLA LIBRERÍA (passlib) ----
# Ajusta "rounds" según tu presupuesto de CPU. 310000 es una buena base hoy.
pwd_context = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto",
    pbkdf2_sha256__rounds=310_000,
)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)  # usa pbkdf2_sha256


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ---- JWT ----
settings = get_settings()


def validate_password_strength(
    password: str,
    *,
    field_name: str = "La contrasena",
) -> str:
    """Validate the shared password policy used across the application."""

    if len(password) < 8:
        raise ValueError(f"{field_name} debe tener al menos 8 caracteres")
    if not any(char.islower() for char in password):
        raise ValueError(f"{field_name} debe incluir al menos una minuscula")
    if not any(char.isupper() for char in password):
        raise ValueError(f"{field_name} debe incluir al menos una mayuscula")
    if not any(char.isdigit() for char in password):
        raise ValueError(f"{field_name} debe incluir al menos un numero")
    if not any(not char.isalnum() for char in password):
        raise ValueError(f"{field_name} debe incluir al menos un caracter especial")
    return password


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    expire = now_in_app_timezone() + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    return jwt.encode({**data, "exp": expire}, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except JWTError as exc:
        raise ValueError("Could not validate credentials") from exc


def generate_secure_password() -> str:
    """Generate a random password between 8 and 12 characters."""

    alphabet = string.ascii_letters + string.digits + string.punctuation
    length = secrets.choice(range(8, 13))

    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        try:
            validate_password_strength(password)
            return password
        except ValueError:
            continue


