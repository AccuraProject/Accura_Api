"""Use case for creating validation rules."""

from typing import Any

from sqlalchemy.orm import Session

from app.domain.entities import Rule
from app.infrastructure.repositories import RuleRepository
from app.utils import now_in_app_timezone
from .artifacts import build_rule_artifacts, normalize_rule_examples_payload
from .validators import ensure_unique_rule_names


def create_rule(
    session: Session,
    *,
    rule: dict[str, Any] | list[Any],
    created_by: int | None = None,
    is_active: bool = True,
) -> Rule:
    """Create a new validation rule."""

    repository = RuleRepository(session)
    rule = normalize_rule_examples_payload(rule)
    ensure_unique_rule_names(rule, repository, created_by=created_by)
    summary, attachment = build_rule_artifacts(rule, rule_id=None)

    now = now_in_app_timezone()
    entity = Rule(
        id=None,
        status="borrador",
        rule=rule,
        summary=summary,
        attachment=attachment,
        created_by=created_by,
        created_at=now,
        updated_by=None,
        updated_at=None,
        is_active=is_active,
        deleted=False,
        deleted_by=None,
        deleted_at=None,
    )
    return repository.create(entity)
