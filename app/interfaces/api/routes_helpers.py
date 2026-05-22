"""Helper utilities shared across API route handlers."""

from dataclasses import dataclass

from app.domain.entities import User


@dataclass(frozen=True)
class CredentialsNotificationDecision:
    """Describe how to notify a user about credential changes."""

    should_send: bool
    include_password: bool


@dataclass(frozen=True)
class PasswordResetRecipient:
    """Describe where password reset credentials should be delivered."""

    email: str | None
    redirected_to_creator: bool


def compute_credentials_notification(
    *,
    email_changed: bool,
    password_changed: bool,
    is_admin: bool,
    acting_on_self: bool,
) -> CredentialsNotificationDecision:
    """Return the notification strategy for a credentials update."""

    if email_changed:
        return CredentialsNotificationDecision(should_send=True, include_password=True)

    if password_changed and is_admin:
        # Administrators receive a confirmation email when changing their own
        # password, but the password itself should not be included because they
        # already know it. When acting on other accounts (which should only
        # happen through a reset) the password must be included.
        return CredentialsNotificationDecision(
            should_send=True,
            include_password=not acting_on_self,
        )

    return CredentialsNotificationDecision(should_send=False, include_password=False)


def resolve_password_reset_recipient(
    target_user: User,
    creator_user: User | None,
) -> PasswordResetRecipient:
    """Return the email recipient respecting the user's send email flag."""

    if target_user.send_emails:
        return PasswordResetRecipient(
            email=target_user.email,
            redirected_to_creator=False,
        )

    return PasswordResetRecipient(
        email=creator_user.email if creator_user is not None else None,
        redirected_to_creator=True,
    )
