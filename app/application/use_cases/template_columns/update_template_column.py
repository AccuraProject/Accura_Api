"""Use case for updating template columns."""

from dataclasses import replace
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.domain.entities import TemplateColumn
from app.infrastructure.dynamic_tables import IdentifierError
from app.infrastructure.repositories import (
    RuleRepository,
    TemplateColumnRepository,
    TemplateRepository,
)
from app.utils import now_in_app_timezone

from .naming import derive_column_identifier, normalize_column_display_name
from .validators import ensure_rule_header_dependencies
from .create_template_column import (
    NewTemplateColumnRuleData,
    _prepare_rule_assignments,
    assign_rule_groups,
)


def update_template_column(
    session: Session,
    *,
    template_id: int,
    column_id: int,
    name: str | None = None,
    description: str | None = None,
    rules: Sequence[NewTemplateColumnRuleData] | None = None,
    rules_provided: bool = False,
    is_active: bool | None = None,
    updated_by: int | None = None,
) -> TemplateColumn:
    """Update an existing template column.

    Raises:
        ValueError: If the template does not exist, is published or the column is missing.
    """

    column_repository = TemplateColumnRepository(session)
    template_repository = TemplateRepository(session)
    rule_repository = RuleRepository(session)

    template = template_repository.get(template_id)
    if template is None:
        raise ValueError("Plantilla no encontrada")

    if template.status == "published":
        raise ValueError("No se pueden modificar las columnas de una plantilla publicada")

    current = column_repository.get(column_id)
    if current is None or current.template_id != template_id:
        raise ValueError("Columna no encontrada")

    new_name = current.name
    if name is not None and name != current.name:
        normalized_name = normalize_column_display_name(name)
        try:
            identifier = derive_column_identifier(normalized_name)
        except IdentifierError as exc:
            raise ValueError(str(exc)) from exc

        existing_columns = column_repository.list_by_template(template_id)
        if any(
            col.id != current.id
            and (
                col.name.lower() == normalized_name.lower()
                or derive_column_identifier(col.name) == identifier
            )
            for col in existing_columns
        ):
            raise ValueError("Ya existe una columna con ese nombre en la plantilla")
        new_name = normalized_name

    new_rules = current.rules
    new_data_type = current.data_type
    if rules_provided:
        new_rules, new_data_type = _prepare_rule_assignments(rule_repository, rules)

    updated_column = replace(
        current,
        name=new_name,
        data_type=new_data_type,
        description=description if description is not None else current.description,
        rules=new_rules,
        is_active=is_active if is_active is not None else current.is_active,
        updated_by=updated_by if updated_by is not None else current.updated_by,
        updated_at=now_in_app_timezone(),
    )

    existing_columns = list(column_repository.list_by_template(template_id))
    updated_columns = [
        updated_column if col.id == updated_column.id else col for col in existing_columns
    ]

    prepared_columns = assign_rule_groups(
        columns=updated_columns,
        rule_repository=rule_repository,
    )

    ensure_rule_header_dependencies(
        columns=prepared_columns,
        rule_repository=rule_repository,
    )

    prepared_column = next(
        column for column in prepared_columns if column.id == updated_column.id
    )
    return column_repository.update(prepared_column)
