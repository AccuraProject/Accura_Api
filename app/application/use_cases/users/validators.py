"""Common validation helpers for user use cases."""


GMAIL_DOMAIN = "gmail.com"


def ensure_valid_email(email: str) -> str:
    """Return a normalized email address or raise ``ValueError``."""

    normalized = email.strip()

    # Validamos que tenga exactamente un arroba
    if normalized.count("@") != 1:
        raise ValueError("El correo electrónico debe tener un formato válido")

    local_part, domain = normalized.split("@", 1)

    # Validamos que tanto la parte local como el dominio no estén vacíos
    if not local_part or not domain:
        raise ValueError("El correo electrónico debe tener un formato válido")

    # Retornamos el correo normalizado (minúsculas)
    return f"{local_part.lower()}@{domain.lower()}"