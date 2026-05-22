"""Use case for updating template access assignments."""

from datetime import date, datetime, time

from sqlalchemy.orm import Session

from app.domain.entities import TemplateUserAccess
from app.infrastructure.repositories import TemplateRepository, TemplateUserAccessRepository
from app.utils import ensure_app_timezone, now_in_app_timezone


def update_template_access(
    session: Session,
    *,
    template_id: int,
    access_id: int,
    start_date: date | datetime | None = None,
    end_date: date | datetime | None = None,
) -> TemplateUserAccess:
    """Update the configured access window for ``access_id``."""

    template = TemplateRepository(session).get(template_id)
    if template is None:
        raise ValueError("Plantilla no encontrada")

    if template.status != "published":
        raise ValueError(
            "No se puede actualizar el acceso porque la plantilla no está publicada"
        )

    repository = TemplateUserAccessRepository(session)
    access = repository.get(access_id)
    if access is None or access.template_id != template_id:
        raise ValueError("Acceso no encontrado")
    if access.revoked_at is not None:
        raise ValueError("No se puede actualizar un acceso revocado")

    normalized_start = (
        _normalize_date(start_date) if start_date is not None else access.start_date
    )
    normalized_end = (
        _normalize_date(end_date, use_end_of_day=True)
        if end_date is not None
        else access.end_date
    )
    _validate_access_window(normalized_start, normalized_end)
    overlapping_access = repository.get_overlapping_access(
        template_id=access.template_id,
        user_id=access.user_id,
        start_date=normalized_start,
        end_date=normalized_end,
        exclude_access_id=access.id,
    )
    if overlapping_access is not None:
        raise ValueError(
            "Ya existe otro acceso vigente o programado que se cruza con el "
            "rango solicitado"
        )

    updated_access = TemplateUserAccess(
        id=access.id,
        template_id=access.template_id,
        user_id=access.user_id,
        start_date=normalized_start,
        end_date=normalized_end,
        revoked_at=access.revoked_at,
        revoked_by=access.revoked_by,
        created_at=access.created_at,
        updated_at=now_in_app_timezone(),
    )

    return repository.update(updated_access)


def _normalize_date(
    value: date | datetime | None, *, use_end_of_day: bool = False
) -> datetime | None:
    """Return a ``datetime`` value normalized to the start or end of the day."""

    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    boundary = time.max if use_end_of_day else time.min
    combined = datetime.combine(value, boundary)
    return ensure_app_timezone(combined)


def _validate_access_window(start: datetime, end: datetime | None) -> None:
    """Validate that the configured access window is chronological."""

    if end is not None and end < start:
        raise ValueError(
            "El rango de fechas no es válido: la fecha de fin debe ser"
            " posterior o igual a la fecha de inicio"
        )


__all__ = ["update_template_access"]
