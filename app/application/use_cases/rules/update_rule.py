"""Use case for updating validation rules."""

from dataclasses import replace
from typing import Any

from sqlalchemy.orm import Session

from app.domain.entities import Rule
from app.infrastructure.storage import delete_blob, extract_blob_path_from_url
from app.infrastructure.repositories import RuleRepository, TemplateColumnRepository
from app.utils import now_in_app_timezone
from .artifacts import build_rule_artifacts
from .validators import ensure_unique_rule_names


def update_rule(
    session: Session,
    *,
    rule_id: int,
    rule: dict[str, Any] | list[Any] | None = None,
    is_active: bool | None = None,
    updated_by: int | None = None,
) -> Rule:
    """Update a validation rule."""

    repository = RuleRepository(session)
    current = repository.get(rule_id)
    if current is None:
        raise ValueError("Regla no encontrada")

    column_repository = TemplateColumnRepository(session)
    if column_repository.rule_used_in_published_template(rule_id):
        raise ValueError(
            "No se puede modificar una regla asignada a una columna de una plantilla publicada."
        )

    new_rule = rule if rule is not None else current.rule
    if rule is not None:
        ensure_unique_rule_names(
            rule,
            repository,
            created_by=current.created_by,
            exclude_rule_id=rule_id,
        )
        old_blob_path = extract_blob_path_from_url(current.attachment or "")
        if old_blob_path:
            delete_blob(old_blob_path)

    summary, attachment = build_rule_artifacts(new_rule, rule_id=rule_id)

    updated_rule = replace(
        current,
        rule=new_rule,
        summary=summary,
        attachment=attachment,
        is_active=is_active if is_active is not None else current.is_active,
        updated_by=updated_by if updated_by is not None else current.updated_by,
        updated_at=now_in_app_timezone(),
    )

    return repository.update(updated_rule)
