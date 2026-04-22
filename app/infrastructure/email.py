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


def _build_email_layout(
    *,
    title: str,
    subtitle: str,
    body_html: str,
    accent_color: str,
    accent_soft: str,
) -> str:
    """Build a branded HTML email layout."""

    return f"""
<!DOCTYPE html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{title}</title>
  </head>
  <body style="margin:0;padding:0;background:#f3f7fb;font-family:Arial,Helvetica,sans-serif;color:#16324f;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f7fb;padding:24px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;background:#ffffff;border-radius:24px;overflow:hidden;box-shadow:0 18px 48px rgba(22,50,79,0.12);">
            <tr>
              <td style="padding:0;background:linear-gradient(135deg,{accent_color} 0%,#16324f 100%);">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                  <tr>
                    <td style="padding:36px 40px 18px 40px;">
                      <div style="display:inline-block;padding:8px 14px;border-radius:999px;background:rgba(255,255,255,0.16);color:#ffffff;font-size:12px;font-weight:bold;letter-spacing:0.08em;text-transform:uppercase;">
                        Accura System
                      </div>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:0 40px 36px 40px;">
                      <h1 style="margin:0 0 10px 0;color:#ffffff;font-size:30px;line-height:1.2;">{title}</h1>
                      <p style="margin:0;color:#dce9f8;font-size:15px;line-height:1.6;">{subtitle}</p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:32px 40px 10px 40px;font-size:15px;line-height:1.7;color:#35506b;">
                {body_html}
              </td>
            </tr>
            <tr>
              <td style="padding:0 40px 36px 40px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{accent_soft};border:1px solid rgba(22,50,79,0.08);border-radius:18px;">
                  <tr>
                    <td style="padding:18px 20px;font-size:13px;line-height:1.6;color:#4f6780;">
                      Este mensaje fue generado automaticamente por Accura. Si no reconoces esta accion, comunicate con el administrador del sistema.
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip()


def _credential_card(*, email: str, password: str | None = None) -> str:
    """Render the email/password summary card."""

    password_row = ""
    if password is not None:
        password_row = (
            "<tr>"
            "<td style=\"padding:0 0 14px 0;font-size:12px;font-weight:bold;letter-spacing:0.06em;text-transform:uppercase;color:#5d7690;\">Contrasena</td>"
            "</tr>"
            "<tr>"
            f"<td style=\"padding:0 0 2px 0;font-size:18px;font-weight:bold;color:#16324f;\">{password}</td>"
            "</tr>"
        )

    return (
        "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" "
        "style=\"margin:24px 0;background:#f8fbff;border:1px solid #d7e6f5;border-radius:18px;\">"
        "<tr><td style=\"padding:22px 24px;\">"
        "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\">"
        "<tr>"
        "<td style=\"padding:0 0 14px 0;font-size:12px;font-weight:bold;letter-spacing:0.06em;text-transform:uppercase;color:#5d7690;\">Correo</td>"
        "</tr>"
        "<tr>"
        f"<td style=\"padding:0 0 18px 0;font-size:18px;font-weight:bold;color:#16324f;\">{email}</td>"
        "</tr>"
        f"{password_row}"
        "</table>"
        "</td></tr></table>"
    )


def send_email(subject: str, html_content: str, recipient: str) -> bool:
    """Send an email using the configured SendGrid credentials."""

    settings = get_settings()
    if not (settings.sendgrid_api_key and settings.sendgrid_sender):
        logger.info("SendGrid configuration incomplete; skipping email delivery")
        return False

    message = Mail(
        from_email=From(settings.sendgrid_sender, "Accura System"),
        to_emails=recipient,
        subject=subject,
        html_content=html_content,
    )
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


def send_new_user_credentials_email(
    email: str,
    password: str,
    *,
    recipient: str | None = None,
) -> bool:
    """Send a welcome email containing the credentials for the new user."""

    recipient_email = recipient or email
    subject = "Bienvenido a Accura"
    html_content = _build_email_layout(
        title="Tu cuenta ya esta lista",
        subtitle="Se creo un nuevo acceso para ti dentro de la plataforma Accura.",
        accent_color="#0f9d7a",
        accent_soft="#edf9f4",
        body_html=(
            "<p style=\"margin:0 0 14px 0;\">Hola,</p>"
            "<p style=\"margin:0 0 14px 0;\">Tu usuario fue creado correctamente. A continuacion encuentras las credenciales iniciales para ingresar.</p>"
            f"{_credential_card(email=email, password=password)}"
            "<p style=\"margin:0;\">Por seguridad, inicia sesion y cambia tu contrasena en cuanto entres a la plataforma.</p>"
        ),
    )
    return send_email(subject, html_content, recipient_email)


def send_user_credentials_update_email(
    email: str,
    password: str | None,
    *,
    recipient: str | None = None,
    email_changed: bool,
    password_changed: bool,
) -> bool:
    """Notify a user about changes to their credentials."""

    recipient_email = recipient or email
    subject = "Actualizacion de credenciales de Accura"
    messages: list[str] = [
        "<p style=\"margin:0 0 14px 0;\">Hola,</p>",
    ]

    if email_changed:
        messages.append(
            "<p style=\"margin:0 0 14px 0;\">Tu correo de acceso fue actualizado correctamente.</p>"
        )

    if password_changed:
        if password is not None:
            messages.append(
                "<p style=\"margin:0 0 14px 0;\">Se genero una nueva contrasena temporal para tu cuenta.</p>"
            )
        else:
            messages.append(
                "<p style=\"margin:0 0 14px 0;\">Tu contrasena fue actualizada correctamente.</p>"
            )
    else:
        messages.append(
            "<p style=\"margin:0 0 14px 0;\">Tu contrasena se mantiene sin cambios.</p>"
        )

    messages.append(_credential_card(email=email, password=password))
    messages.append(
        "<p style=\"margin:0;\">Si no reconoces esta actualizacion, comunicate cuanto antes con el administrador.</p>"
    )
    html_content = _build_email_layout(
        title="Actualizamos tus credenciales",
        subtitle="Estos son los datos vigentes para tu acceso a Accura.",
        accent_color="#1d6fd8",
        accent_soft="#eef5ff",
        body_html="".join(messages),
    )
    return send_email(subject, html_content, recipient_email)


def send_user_password_reset_email(
    email: str,
    password: str,
    *,
    recipient: str | None = None,
) -> bool:
    """Send a password reset email with the generated credentials."""

    recipient_email = recipient or email
    subject = "Restablecimiento de contrasena de Accura"
    html_content = _build_email_layout(
        title="Restablecimos tu contrasena",
        subtitle="Usa esta credencial temporal para volver a entrar a tu cuenta.",
        accent_color="#f28c28",
        accent_soft="#fff4e9",
        body_html="".join(
            (
                "<p style=\"margin:0 0 14px 0;\">Hola,</p>",
                "<p style=\"margin:0 0 14px 0;\">Se genero una nueva contrasena temporal para tu cuenta.</p>",
                f"{_credential_card(email=email, password=password)}",
                "<p style=\"margin:0;\">Por seguridad, inicia sesion y actualiza tu contrasena lo antes posible.</p>",
            )
        ),
    )
    return send_email(subject, html_content, recipient_email)
