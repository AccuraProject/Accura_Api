"""Persistence layer for validation rules."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import desc, false
from sqlalchemy.orm import Session

from app.domain.entities import Rule
from app.infrastructure.models import RuleModel, TemplateColumnModel, TemplateModel
from app.infrastructure.models.template_column import template_column_rule_table
from app.utils import ensure_app_naive_datetime, now_in_app_timezone

RULE_STATUS_DRAFT = "borrador"
RULE_STATUS_ASSIGNED = "asignada"


class RuleRepository:
    """Provide CRUD operations for validation rules."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        creator_id: int | None = None,
    ) -> Sequence[Rule]:
        query = self.session.query(RuleModel).filter(RuleModel.deleted == false())
        if creator_id is not None:
            query = query.filter(RuleModel.created_by == creator_id)
        query = query.order_by(desc(RuleModel.created_at), desc(RuleModel.id))
        if skip:
            query = query.offset(skip)
        if limit is not None:
            query = query.limit(limit)
        return [self._to_entity(model) for model in query.all()]

    def list_recent(
        self, *, limit: int = 5, creator_id: int | None = None
    ) -> Sequence[Rule]:
        query = self.session.query(RuleModel).filter(RuleModel.deleted == false())
        if creator_id is not None:
            query = query.filter(RuleModel.created_by == creator_id)
        query = query.order_by(desc(RuleModel.id))
        if limit is not None:
            query = query.limit(limit)
        return [self._to_entity(model) for model in query.all()]

    def list_recent_by_type(
        self,
        *,
        limit: int = 5,
        creator_id: int | None = None,
        rule_types: Sequence[str] = (),
    ) -> Sequence[Rule]:
        """Return the most recent rules that match any of the provided types."""

        normalized_types = {
            self._normalize_label(value)
            for value in rule_types
            if isinstance(value, str) and value.strip()
        }
        if not normalized_types:
            return []

        base_query = self.session.query(RuleModel).filter(RuleModel.deleted == false())
        if creator_id is not None:
            base_query = base_query.filter(RuleModel.created_by == creator_id)
        base_query = base_query.order_by(desc(RuleModel.id))

        if limit is None:
            search_batch = 20
        else:
            search_batch = max(limit * 4, 20)

        matches: list[Rule] = []
        offset = 0

        while True:
            query = base_query.offset(offset)
            if search_batch:
                query = query.limit(search_batch)
            models = query.all()
            if not models:
                break

            for model in models:
                entity = self._to_entity(model)
                if self._rule_contains_type(entity.rule, normalized_types):
                    matches.append(entity)
                    if limit is not None and len(matches) >= limit:
                        return matches

            if len(models) < search_batch:
                break
            offset += search_batch

        return matches

    def list_by_creator(self, creator_id: int) -> Sequence[Rule]:
        query = (
            self.session.query(RuleModel)
            .filter(RuleModel.deleted == false())
            .filter(RuleModel.created_by == creator_id)
            .order_by(desc(RuleModel.created_at))
        )
        return [self._to_entity(model) for model in query.all()]

    def get(self, rule_id: int) -> Rule | None:
        model = self._get_model(id=rule_id)
        return self._to_entity(model) if model else None

    def get_active_map(self, rule_ids: Sequence[int]) -> dict[int, Any]:
        """Return active rule payloads indexed by id for the provided identifiers."""

        normalized_ids = sorted({int(rule_id) for rule_id in rule_ids if rule_id})
        if not normalized_ids:
            return {}

        models = (
            self.session.query(RuleModel)
            .filter(
                RuleModel.deleted == false(),
                RuleModel.is_active.is_(True),
                RuleModel.id.in_(normalized_ids),
            )
            .all()
        )
        return {model.id: model.rule for model in models}

    def create(self, rule: Rule) -> Rule:
        model = RuleModel()
        self._apply_entity_to_model(model, rule)
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        return self._to_entity(model)

    def update(self, rule: Rule) -> Rule:
        model = self._get_model(id=rule.id)
        if not model:
            msg = f"Rule with id {rule.id} not found"
            raise ValueError(msg)
        self._apply_entity_to_model(model, rule)
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        return self._to_entity(model)

    def delete(self, rule_id: int, *, deleted_by: int | None = None) -> None:
        model = self._get_model(id=rule_id, include_deleted=True)
        if not model:
            msg = f"Rule with id {rule_id} not found"
            raise ValueError(msg)
        if model.deleted:
            return
        now = ensure_app_naive_datetime(now_in_app_timezone())
        model.deleted = True
        model.deleted_by = deleted_by
        model.deleted_at = now
        model.is_active = False
        model.updated_by = deleted_by
        model.updated_at = now
        self.session.add(model)
        self.session.commit()

    @staticmethod
    def _to_entity(model: RuleModel) -> Rule:
        return Rule(
            id=model.id,
            status=model.status or RULE_STATUS_DRAFT,
            rule=model.rule,
            summary=model.summary,
            attachment=model.attachment,
            created_by=model.created_by,
            created_at=ensure_app_naive_datetime(model.created_at),
            updated_by=model.updated_by,
            updated_at=ensure_app_naive_datetime(model.updated_at),
            is_active=model.is_active,
            deleted=model.deleted,
            deleted_by=model.deleted_by,
            deleted_at=ensure_app_naive_datetime(model.deleted_at),
        )

    def _get_model(self, include_deleted: bool = False, **filters) -> RuleModel | None:
        query = self.session.query(RuleModel)
        if not include_deleted:
            query = query.filter(RuleModel.deleted == false())
        return query.filter_by(**filters).first()

    def find_conflicting_rule_name(
        self,
        candidate_names: Sequence[str],
        *,
        created_by: int | None = None,
        exclude_rule_id: int | None = None,
    ) -> str | None:
        normalized_candidates = {
            name.strip().lower()
            for name in candidate_names
            if isinstance(name, str) and name.strip()
        }
        if not normalized_candidates:
            return None

        query = self.session.query(RuleModel.id, RuleModel.rule).filter(
            RuleModel.deleted == false()
        )
        if created_by is None:
            query = query.filter(RuleModel.created_by.is_(None))
        else:
            query = query.filter(RuleModel.created_by == created_by)
        if exclude_rule_id is not None:
            query = query.filter(RuleModel.id != exclude_rule_id)

        for _, rule_data in query.all():
            for existing_name in self._extract_rule_names(rule_data):
                if existing_name.lower() in normalized_candidates:
                    return existing_name
        return None

    @staticmethod
    def _apply_entity_to_model(model: RuleModel, rule: Rule) -> None:
        model.status = rule.status
        model.rule = rule.rule
        model.summary = rule.summary
        model.attachment = rule.attachment
        model.created_by = rule.created_by
        if rule.created_at is not None:
            model.created_at = ensure_app_naive_datetime(rule.created_at)
        model.updated_by = rule.updated_by
        model.updated_at = ensure_app_naive_datetime(rule.updated_at)
        model.is_active = rule.is_active
        model.deleted = rule.deleted
        model.deleted_by = rule.deleted_by
        model.deleted_at = ensure_app_naive_datetime(rule.deleted_at)

    @staticmethod
    def _extract_rule_names(rule_data) -> list[str]:
        names: list[str] = []
        RuleRepository._collect_rule_names(rule_data, names)

        unique_names: list[str] = []
        seen: set[str] = set()
        for name in names:
            normalized = name.lower()
            if normalized not in seen:
                seen.add(normalized)
                unique_names.append(name)
        return unique_names

    @staticmethod
    def _collect_rule_names(rule_data, names: list[str]) -> None:
        if isinstance(rule_data, Mapping):
            raw_name = rule_data.get("Nombre de la regla")
            if isinstance(raw_name, str):
                stripped = raw_name.strip()
                if stripped:
                    names.append(stripped)
            for value in rule_data.values():
                RuleRepository._collect_rule_names(value, names)
            return

        if isinstance(rule_data, Sequence) and not isinstance(rule_data, (str, bytes)):
            for entry in rule_data:
                RuleRepository._collect_rule_names(entry, names)

    @staticmethod
    def _rule_contains_type(rule_data: Any, normalized_targets: set[str]) -> bool:
        if isinstance(rule_data, Mapping):
            type_label = rule_data.get("Tipo de dato")
            if isinstance(type_label, str):
                normalized = RuleRepository._normalize_label(type_label)
                if normalized in normalized_targets:
                    return True
            for value in rule_data.values():
                if RuleRepository._rule_contains_type(value, normalized_targets):
                    return True
            return False

        if isinstance(rule_data, Sequence) and not isinstance(rule_data, (str, bytes)):
            return any(
                RuleRepository._rule_contains_type(entry, normalized_targets)
                for entry in rule_data
            )

        return False

    @staticmethod
    def _normalize_label(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(value))
        ascii_label = "".join(char for char in normalized if not unicodedata.combining(char))
        collapsed = re.sub(r"[\s\-_/]+", " ", ascii_label)
        return collapsed.lower().strip()

    def refresh_statuses(self, rule_ids: Sequence[int] | None = None) -> None:
        query = self.session.query(RuleModel).filter(RuleModel.deleted == false())
        if rule_ids is not None:
            normalized_ids = sorted({int(rule_id) for rule_id in rule_ids if rule_id})
            if not normalized_ids:
                return
            query = query.filter(RuleModel.id.in_(normalized_ids))

        models = query.all()
        if not models:
            return

        assigned_ids = self._assigned_rule_ids(rule_ids=[model.id for model in models])
        changed = False
        for model in models:
            expected_status = (
                RULE_STATUS_ASSIGNED if model.id in assigned_ids else RULE_STATUS_DRAFT
            )
            if model.status != expected_status:
                model.status = expected_status
                self.session.add(model)
                changed = True

        if changed:
            self.session.commit()

    def _assigned_rule_ids(self, rule_ids: Sequence[int] | None = None) -> set[int]:
        query = (
            self.session.query(RuleModel.id)
            .join(
                template_column_rule_table,
                template_column_rule_table.c.rule_id == RuleModel.id,
            )
            .join(
                TemplateColumnModel,
                TemplateColumnModel.id
                == template_column_rule_table.c.template_column_id,
            )
            .join(TemplateModel, TemplateModel.id == TemplateColumnModel.template_id)
            .filter(
                RuleModel.deleted == false(),
                TemplateColumnModel.deleted == false(),
                TemplateModel.deleted == false(),
                TemplateModel.status == "published",
            )
        )
        if rule_ids is not None:
            normalized_ids = sorted({int(rule_id) for rule_id in rule_ids if rule_id})
            if not normalized_ids:
                return set()
            query = query.filter(RuleModel.id.in_(normalized_ids))
        return {rule_id for (rule_id,) in query.distinct().all()}


__all__ = ["RuleRepository"]
