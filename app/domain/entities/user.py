"""Domain entity representing a user."""

from dataclasses import dataclass
from datetime import datetime


from .role import Role


@dataclass
class User:
    """Core attributes describing an application user."""

    id: int | None
    role: Role
    name: str
    email: str
    password: str
    send_emails: bool
    must_change_password: bool
    last_login: datetime | None
    created_by: int | None
    created_at: datetime | None
    updated_by: int | None
    updated_at: datetime | None
    is_active: bool
    deleted: bool
    deleted_by: int | None
    deleted_at: datetime | None

    def has_role(self, alias: str) -> bool:
        """Return ``True`` when the user's role alias matches ``alias``."""

        return self.role.alias.lower() == alias.lower()

    def is_admin(self) -> bool:
        """Return ``True`` when the user is an administrator."""

        return self.has_role("admin")
