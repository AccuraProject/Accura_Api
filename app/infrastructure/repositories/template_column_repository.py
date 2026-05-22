"""Persistence layer for template columns."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import false
from sqlalchemy.orm import Session, joinedload

from app.domain.entities import TemplateColumn, TemplateColumnRule
from app.infrastructure.models import (
    RuleModel,
    TemplateColumnModel,
    TemplateModel,
    template_column_rule_table,
)
from app.utils import ensure_app_naive_datetime, now_in_app_timezone


class TemplateColumnRepository:
    """Provide CRUD operations for template columns."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_by_template(self, template_id: int) -> Sequence[TemplateColumn]:
        query = (
            self.session.query(TemplateColumnModel)
            .options(joinedload(TemplateColumnModel.rules))
            .filter(TemplateColumnModel.template_id == template_id)
            .filter(TemplateColumnModel.deleted == false())
            .order_by(TemplateColumnModel.id.asc())
        )
        return [self._to_entity(model) for model in query.all()]

    def get(self, column_id: int) -> TemplateColumn | None:
        model = self._get_model(id=column_id)
        return self._to_entity(model) if model else None

    def create(self, column: TemplateColumn) -> TemplateColumn:
        model = TemplateColumnModel()
        self._apply_entity_to_model(model, column, include_creation_fields=True)
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        if model.rules:
            self.session.refresh(model, attribute_names=["rules"])
        return self._to_entity(model)

    def create_many(self, columns: Sequence[TemplateColumn]) -> list[TemplateColumn]:
        models: list[TemplateColumnModel] = []
        for column in columns:
            model = TemplateColumnModel()
            self._apply_entity_to_model(model, column, include_creation_fields=True)
            self.session.add(model)
            models.append(model)

        self.session.commit()

        for model in models:
            self.session.refresh(model)
            if model.rules:
                self.session.refresh(model, attribute_names=["rules"])

        return [self._to_entity(model) for model in models]

    def update(self, column: TemplateColumn) -> TemplateColumn:
        model = self._get_model(id=column.id)
        if not model:
            msg = f"Template column with id {column.id} not found"
            raise ValueError(msg)
        self._apply_entity_to_model(model, column, include_creation_fields=False)
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        if model.rules:
            self.session.refresh(model, attribute_names=["rules"])
        return self._to_entity(model)

    def delete(self, column_id: int, *, deleted_by: int | None = None) -> None:
        model = self._get_model(id=column_id, include_deleted=True)
        if not model:
            msg = f"Template column with id {column_id} not found"
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
    def _to_entity(model: TemplateColumnModel) -> TemplateColumn:
        headers_map, fallback_headers = TemplateColumnRepository._deserialize_rule_headers(
            model.rule_header
        )
        rules = []
        for rule_model in model.rules:
            if getattr(rule_model, "deleted", False):
                continue
            headers = headers_map.get(rule_model.id)
            if headers is None and fallback_headers is not None:
                headers = fallback_headers
            rules.append(TemplateColumnRule(id=rule_model.id, headers=headers))

        return TemplateColumn(
            id=model.id,
            template_id=model.template_id,
            rules=tuple(rules),
            name=model.name,
            description=model.description,
            data_type=model.data_type,
            created_by=model.created_by,
            created_at=ensure_app_naive_datetime(model.created_at),
            updated_by=model.updated_by,
            updated_at=ensure_app_naive_datetime(model.updated_at),
            is_active=model.is_active,
            deleted=model.deleted,
            deleted_by=model.deleted_by,
            deleted_at=ensure_app_naive_datetime(model.deleted_at),
        )

    def _get_model(self, include_deleted: bool = False, **filters) -> TemplateColumnModel | None:
        query = self.session.query(TemplateColumnModel).options(
            joinedload(TemplateColumnModel.rules)
        )
        if not include_deleted:
            query = query.filter(TemplateColumnModel.deleted == false())
        return query.filter_by(**filters).first()

    def _apply_entity_to_model(
        self,
        model: TemplateColumnModel,
        column: TemplateColumn,
        *,
        include_creation_fields: bool,
    ) -> None:
        if include_creation_fields:
            model.created_by = column.created_by
            model.created_at = (
                ensure_app_naive_datetime(column.created_at)
                or ensure_app_naive_datetime(now_in_app_timezone())
            )
            model.updated_by = None
            model.updated_at = None
        model.template_id = column.template_id
        model.rules = self._load_rules(column.rule_ids)
        model.rule_header = self._serialize_rule_headers(column.rules)
        model.name = column.name
        model.description = column.description
        model.data_type = column.data_type
        if not include_creation_fields:
            model.updated_by = column.updated_by
            model.updated_at = ensure_app_naive_datetime(column.updated_at)
        model.is_active = column.is_active
        model.deleted = column.deleted
        model.deleted_by = column.deleted_by
        model.deleted_at = ensure_app_naive_datetime(column.deleted_at)

    def _load_rules(self, rule_ids: tuple[int, ...]) -> list[RuleModel]:
        if not rule_ids:
            return []

        unique_ids: list[int] = []
        seen: set[int] = set()
        for rule_id in rule_ids:
            if rule_id in seen:
                continue
            seen.add(rule_id)
            unique_ids.append(rule_id)
        rule_models = (
            self.session.query(RuleModel)
            .filter(RuleModel.id.in_(unique_ids))
            .all()
        )
        found = {rule.id: rule for rule in rule_models}
        missing = [rule_id for rule_id in unique_ids if rule_id not in found]
        if missing:
            missing_str = ", ".join(str(rule_id) for rule_id in missing)
            raise ValueError(f"Las reglas {missing_str} no existen")

        return [found[rule_id] for rule_id in unique_ids]

    @staticmethod
    def _serialize_rule_headers(
        rules: Sequence[TemplateColumnRule],
    ) -> list[dict[str, Any]] | None:
        serialized: list[dict[str, Any]] = []
        for assignment in rules:
            if not assignment.headers:
                continue
            entries = list(assignment.headers)
            serialized.append(
                {
                    "rule_id": assignment.id,
                    "Header rule": entries,
                }
            )
        return serialized or None

    @staticmethod
    def _deserialize_rule_headers(
        raw_headers: Any,
    ) -> tuple[dict[int, tuple[str, ...]], tuple[str, ...] | None]:
        if not raw_headers:
            return {}, None

        fallback: tuple[str, ...] | None = None
        headers_map: dict[int, tuple[str, ...]] = {}

        if isinstance(raw_headers, list):
            # Handle legacy storage of a flat list of headers
            if all(isinstance(entry, str) for entry in raw_headers):
                values = [entry.strip() for entry in raw_headers if isinstance(entry, str)]
                fallback = tuple(value for value in values if value)
                return headers_map, fallback or None

            for entry in raw_headers:
                if not isinstance(entry, dict):
                    continue
                raw_id = entry.get("rule_id") or entry.get("id")
                try:
                    rule_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                headers = (
                    entry.get("Header rule")
                )
                if isinstance(headers, str):
                    candidates = [headers]
                elif isinstance(headers, list):
                    candidates = [value for value in headers if isinstance(value, str)]
                else:
                    continue
                normalized = [value.strip() for value in candidates if value and value.strip()]
                if normalized:
                    headers_map[rule_id] = tuple(normalized)
            return headers_map, None

        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                try:
                    rule_id = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, str):
                    normalized = value.strip()
                    if normalized:
                        headers_map[rule_id] = (normalized,)
                elif isinstance(value, list):
                    normalized_values = [
                        item.strip() for item in value if isinstance(item, str) and item.strip()
                    ]
                    if normalized_values:
                        headers_map[rule_id] = tuple(normalized_values)
            return headers_map, None

        if isinstance(raw_headers, str):
            normalized = raw_headers.strip()
            fallback = (normalized,) if normalized else None

        return headers_map, fallback

    def is_rule_in_use(self, rule_id: int) -> bool:
        """Return ``True`` when a rule is linked to any template column."""

        query = (
            self.session.query(template_column_rule_table.c.template_column_id)
            .join(
                TemplateColumnModel,
                TemplateColumnModel.id
                == template_column_rule_table.c.template_column_id,
            )
            .join(TemplateModel, TemplateModel.id == TemplateColumnModel.template_id)
            .filter(
                template_column_rule_table.c.rule_id == rule_id,
                TemplateColumnModel.deleted == false(),
                TemplateModel.deleted == false(),
            )
        )
        return query.first() is not None

    def rule_used_in_published_template(self, rule_id: int) -> bool:
        """Return ``True`` if a rule is assigned to a column of a published template."""

        query = (
            self.session.query(TemplateColumnModel.id)
            .join(TemplateModel, TemplateModel.id == TemplateColumnModel.template_id)
            .join(
                template_column_rule_table,
                template_column_rule_table.c.template_column_id
                == TemplateColumnModel.id,
            )
            .filter(
                template_column_rule_table.c.rule_id == rule_id,
                TemplateColumnModel.deleted == false(),
                TemplateModel.deleted == false(),
                TemplateModel.status == "published",
            )
        )
        return query.first() is not None


__all__ = ["TemplateColumnRepository"]
