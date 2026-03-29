"""Utility helpers for sending transactional email notifications via SendGrid."""

from __future__ import annotations

import json
import logging
from typing import Any

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, From, ReplyTo

from app.config import get_settings

logger = logging.getLogger(__name__)


def _extract_sendgrid_error_details(body: Any) -> str | None:
    """Return a human readable description for a SendGrid error payload."""

    if body in (None, ""):
        return None

    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return None

    if isinstance(body, str):
        body = body.strip()
        if not body:
            return None
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return body
    else:
        parsed = body

    if isinstance(parsed, dict):
        errors = parsed.get("errors")
        if isinstance(errors, list):
            messages: list[str] = []
            for item in errors:
                if not isinstance(item, dict):
                    continue
                message = item.get("message")
                help_link = item.get("help")
                if message and help_link:
                    messages.append(f"{message} (help: {help_link})")
                elif message:
                    messages.append(str(message))
            if messages:
                return "; ".join(messages)
        # Fall back to a JSON string for unrecognised payloads
        try:
            return json.dumps(parsed, ensure_ascii=False)
        except (TypeError, ValueError):
            return None

    if isinstance(parsed, list):
        try:
            return "; ".join(str(item) for item in parsed)
        except TypeError:
            return None

    return None


def _log_sendgrid_exception(exc: Exception) -> None:
    """Log a SendGrid API error with helpful troubleshooting details."""

    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    details = _extract_sendgrid_error_details(body)

    if status_code and details:
        logger.error(
            "SendGrid API request failed with status %s: %s", status_code, details
        )
    elif status_code:
        logger.error("SendGrid API request failed with status %s", status_code)
    elif details:
        logger.error("SendGrid API request failed: %s", details)
    else:
        logger.exception("Error sending email via SendGrid: %s", exc)


def _log_unsuccessful_response(response: Any) -> None:
    """Log details from an unsuccessful SendGrid response object."""

    status_code = getattr(response, "status_code", None)
    body = getattr(response, "body", None)
    details = _extract_sendgrid_error_details(body)

    if details:
        logger.error(
            "SendGrid API responded with status %s: %s", status_code, details
        )
    else:
        logger.error("SendGrid API responded with status %s", status_code)


def send_email(subject: str, html_content: str, recipient: str) -> bool:
    """Send an email using the configured SendGrid credentials."""

    settings = get_settings()
    if not (settings.sendgrid_api_key and settings.sendgrid_sender):
        logger.info("SendGrid configuration incomplete; skipping email delivery")
        return False

    message = Mail(
        from_email=From(settings.sendgrid_sender, "Accura System"),  # "Accura System" ayuda
        to_emails=recipient,
        subject=subject,
        html_content=html_content,
    )
    # Esto permite que te respondan a tu Gmail personal
    message.reply_to = ReplyTo("deyvidjosephg@gmail.com", "Soporte Deyvid")
    try:
        client = SendGridAPIClient(settings.sendgrid_api_key)
        response = client.send(message)
    except Exception as exc:  # pragma: no cover - network failures depend on environment
        _log_sendgrid_exception(exc)
        return False

    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        _log_unsuccessful_response(response)
        return False

    return True


def send_new_user_credentials_email(email: str, password: str) -> bool:
    """Send a welcome email containing the credentials for the new user."""

    subject = "Bienvenido a Accura"
    html_content = (
        "<p>Hola,</p>"
        "<p>Tu usuario ha sido creado correctamente.</p>"
        f"<p><strong>Correo:</strong> {email}<br><strong>Contraseña:</strong> {password}</p>"
        "<p>Por favor, inicia sesión y actualiza tu contraseña lo antes posible.</p>"
    )
    return send_email(subject, html_content, email)


def send_user_credentials_update_email(
    email: str,
    password: str | None,
    *,
    email_changed: bool,
    password_changed: bool,
) -> bool:
    """Notify a user about changes to their credentials."""

    subject = "Actualización de credenciales de Accura"
    messages: list[str] = ["<p>Hola,</p>"]

    if email_changed:
        messages.append(
            "<p>Tu correo electrónico de acceso ha sido actualizado correctamente.</p>"
        )

    if password_changed:
        if password is not None:
            messages.append(
                (
                    "<p>Se generó una nueva contraseña temporal para tu cuenta.</p>"
                    f"<p><strong>Contraseña:</strong> {password}</p>"
                )
            )
        else:
            messages.append(
                "<p>Tu contraseña fue actualizada correctamente.</p>"
            )
    else:
        messages.append(
            "<p>Tu contraseña se mantiene sin cambios.</p>"
        )

    messages.append(
        "<p>Si tú no solicitaste esta actualización, por favor comunícate con el administrador.</p>"
    )
    html_content = "".join(messages)
    return send_email(subject, html_content, email)


def send_user_password_reset_email(email: str, password: str) -> bool:
    """Send a password reset email with the generated credentials."""

    subject = "Restablecimiento de contraseña de Accura"
    html_content = "".join(
        (
            "<p>Hola,</p>",
            "<p>Se generó una nueva contraseña temporal para tu cuenta.</p>",
            f"<p><strong>Contraseña:</strong> {password}</p>",
            "<p>Por seguridad, inicia sesión y actualiza tu contraseña lo antes posible.</p>",
        )
    )
    return send_email(subject, html_content, email)

