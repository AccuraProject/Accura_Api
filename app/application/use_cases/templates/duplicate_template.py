"""Use case to duplicate an existing template."""

from sqlalchemy.orm import Session

from app.application.use_cases.template_columns.artifacts import refresh_template_resources
from app.application.use_cases.template_columns.create_template_column import (
    assign_rule_groups,
)
from app.application.use_cases.template_columns.validators import ensure_rule_header_dependencies
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


def _build_copy_value(
    value: str, *, max_length: int, duplicate_number: int = 0
) -> str:
    base_value = (value or "").strip()
    suffix = _COPY_SUFFIX
    if duplicate_number > 0:
        suffix = f"{_COPY_SUFFIX}({duplicate_number})"
    allowed_base_length = max_length - len(suffix)
    if allowed_base_length <= 0:
        return suffix[:max_length]
    return f"{base_value[:allowed_base_length]}{suffix}"


def _build_copy_table_name(
    value: str, *, max_length: int, duplicate_number: int = 0
) -> str:
    base_value = (value or "").strip()
    suffix = _COPY_SUFFIX
    if duplicate_number > 0:
        suffix = f"{_COPY_SUFFIX}_{duplicate_number}"
    allowed_base_length = max_length - len(suffix)
    if allowed_base_length <= 0:
        return suffix[:max_length].lower()
    return f"{base_value[:allowed_base_length]}{suffix}".lower()


def _next_available_copy_name(
    repository: TemplateRepository,
    source_name: str,
    *,
    created_by: int | None,
) -> str:
    duplicate_number = 0
    while True:
        candidate = _build_copy_value(
            source_name,
            max_length=_MAX_TEMPLATE_NAME_LENGTH,
            duplicate_number=duplicate_number,
        )
        if repository.get_by_name(candidate, created_by=created_by) is None:
            return candidate
        duplicate_number += 1


def _next_available_copy_table_name(
    repository: TemplateRepository,
    source_table_name: str,
) -> str:
    duplicate_number = 0
    while True:
        candidate = _build_copy_table_name(
            source_table_name,
            max_length=_MAX_TABLE_NAME_LENGTH,
            duplicate_number=duplicate_number,
        )
        if repository.get_by_table_name(candidate) is None:
            return candidate
        duplicate_number += 1


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

    duplicated_name = _next_available_copy_name(
        template_repository,
        source_template.name,
        created_by=created_by,
    )
    duplicated_table_name = _next_available_copy_table_name(
        template_repository,
        source_template.table_name,
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

    prepared_columns = assign_rule_groups(
        columns=new_columns,
        rule_repository=rule_repository,
    )

    ensure_rule_header_dependencies(
        columns=prepared_columns,
        rule_repository=rule_repository,
    )

    column_repository.create_many(prepared_columns)
    refresh_template_resources(
        session,
        duplicated_template.id,
        actor_id=created_by,
    )

    # Refresh the duplicated template so it includes the cloned columns.
    return template_repository.get(duplicated_template.id)


__all__ = ["duplicate_template"]
