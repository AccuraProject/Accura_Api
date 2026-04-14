"""Use case to duplicate an existing template."""

from sqlalchemy.orm import Session

from app.application.use_cases.template_columns.artifacts import refresh_template_resources
from app.application.use_cases.template_columns.validators import (
    ensure_rule_header_dependencies,
)
from app.application.use_cases.templates.create_template import create_template
from app.domain.entities import Template, TemplateColumn
from app.infrastructure.repositories import (
    RuleRepository,
    TemplateColumnRepository,
    TemplateRepository,
)
from app.utils import now_in_app_timezone

_COPY_SUFFIX = "_Copy"
_MAX_TEMPLATE_NAME_LENGTH = 50
_MAX_TABLE_NAME_LENGTH = 63


def _build_copy_value(value: str, *, max_length: int) -> str:
    base_value = (value or "").strip()
    allowed_base_length = max_length - len(_COPY_SUFFIX)
    if allowed_base_length <= 0:
        return _COPY_SUFFIX[:max_length]
    return f"{base_value[:allowed_base_length]}{_COPY_SUFFIX}"


def duplicate_template(
    session: Session,
    *,
    template_id: int,
    created_by: int | None = None,
) -> Template:
    """Duplicate ``template_id`` reusing its metadata and columns."""

    template_repository = TemplateRepository(session)
    source_template = template_repository.get(template_id)
    if source_template is None:
        raise ValueError("Plantilla no encontrada")

    duplicated_name = _build_copy_value(
        source_template.name,
        max_length=_MAX_TEMPLATE_NAME_LENGTH,
    )
    duplicated_table_name = _build_copy_value(
        source_template.table_name,
        max_length=_MAX_TABLE_NAME_LENGTH,
    )

    duplicated_template = create_template(
        session,
        user_id=source_template.user_id,
        name=duplicated_name,
        table_name=duplicated_table_name,
        description=source_template.description,
        created_by=created_by,
    )

    if not source_template.columns:
        return duplicated_template

    rule_repository = RuleRepository(session)
    column_repository = TemplateColumnRepository(session)
    now = now_in_app_timezone()
    new_columns: list[TemplateColumn] = []
    for column in source_template.columns:
        new_columns.append(
            TemplateColumn(
                id=None,
                template_id=duplicated_template.id,
                rules=column.rules,
                name=column.name,
                description=column.description,
                data_type=column.data_type,
                created_by=created_by,
                created_at=now,
                updated_by=None,
                updated_at=None,
                is_active=column.is_active,
                deleted=False,
                deleted_by=None,
                deleted_at=None,
            )
        )

    ensure_rule_header_dependencies(
        columns=new_columns,
        rule_repository=rule_repository,
    )

    column_repository.create_many(new_columns)
    refresh_template_resources(
        session,
        duplicated_template.id,
        actor_id=created_by,
    )

    # Refresh the duplicated template so it includes the cloned columns.
    return template_repository.get(duplicated_template.id)


__all__ = ["duplicate_template"]
