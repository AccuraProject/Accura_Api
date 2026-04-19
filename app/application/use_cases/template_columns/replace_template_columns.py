"""Use case for replacing all template columns with new definitions."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.domain.entities import TemplateColumn
from app.infrastructure.repositories import (
    RuleRepository,
    TemplateColumnRepository,
    TemplateRepository,
)

from .create_template_column import (
    NewTemplateColumnData,
    _build_column,
    create_template_columns,
)
from .artifacts import refresh_template_resources
from .validators import ensure_rule_header_dependencies


def replace_template_columns(
    session: Session,
    *,
    template_id: int,
    columns: Sequence[NewTemplateColumnData],
    actor_id: int | None = None,
) -> list[TemplateColumn]:
    """Replace all columns of a template with the provided definitions.

    When ``columns`` is empty, preserve the current columns instead of deleting
    them. This avoids accidental data loss when the client saves template
    metadata without persisting the column editor state.
    """

    column_repository = TemplateColumnRepository(session)
    template_repository = TemplateRepository(session)

    template = template_repository.get(template_id)
    if template is None:
        raise ValueError("Plantilla no encontrada")

    if template.status == "published":
        raise ValueError("No se pueden modificar las columnas de una plantilla publicada")

    existing_columns = list(column_repository.list_by_template(template_id))
    if not columns:
        return existing_columns

    rule_repository = RuleRepository(session)
    forbidden_names: set[str] = set()
    forbidden_identifiers: set[str] = set()
    validated_columns: list[TemplateColumn] = []

    for payload in columns:
        validated_columns.append(
            _build_column(
                template_id=template_id,
                payload=payload,
                created_by=actor_id,
                forbidden_names=forbidden_names,
                forbidden_identifiers=forbidden_identifiers,
                rule_repository=rule_repository,
            )
        )

    ensure_rule_header_dependencies(
        columns=validated_columns,
        rule_repository=rule_repository,
    )

    for column in existing_columns:
        column_repository.delete(column.id, deleted_by=actor_id)

    created_columns: list[TemplateColumn] = []
    if columns:
        created_columns = create_template_columns(
            session,
            template_id=template_id,
            columns=columns,
            created_by=actor_id,
        )

    refresh_template_resources(
        session,
        template_id,
        actor_id=actor_id,
    )

    return created_columns


__all__ = [
    "replace_template_columns",
]
