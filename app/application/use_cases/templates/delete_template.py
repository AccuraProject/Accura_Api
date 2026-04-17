"""Use case for deleting templates."""

from sqlalchemy.orm import Session

from app.domain.entities import AuditLog
from app.infrastructure.dynamic_tables import drop_template_table
from app.infrastructure.template_files import delete_template_excel
from app.infrastructure.repositories import (
    AuditLogRepository,
    DigitalFileRepository,
    RuleRepository,
    TemplateRepository,
)
from app.utils import now_in_app_timezone


def delete_template(
    session: Session, template_id: int, *, deleted_by: int | None = None
) -> None:
    """Delete the template and its dynamic table if necessary."""

    repository = TemplateRepository(session)
    rule_repository = RuleRepository(session)
    template = repository.get(template_id)
    if template is None:
        raise ValueError("Plantilla no encontrada")
    affected_rule_ids = {
        rule.id
        for column in template.columns
        for rule in column.rules
    }

    digital_file_repository = DigitalFileRepository(session)
    existing_digital_file = digital_file_repository.get_by_template_id(template.id)

    repository.delete(template_id, deleted_by=deleted_by)
    if affected_rule_ids:
        rule_repository.refresh_statuses(affected_rule_ids)

    try:
        drop_template_table(template.table_name)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc

    if existing_digital_file is not None:
        delete_template_excel(existing_digital_file.path)
    digital_file_repository.delete_by_template_id(template.id)

    if template.status == "published":
        audit_repository = AuditLogRepository(session)
        audit_repository.create(
            AuditLog(
                id=None,
                template_name=template.name,
                columns=[column.name for column in template.columns],
                operation="eliminacion",
                created_by=deleted_by,
                created_at=now_in_app_timezone(),
                updated_by=None,
                updated_at=None,
            )
        )
