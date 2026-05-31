"""Persistence layer for templates."""

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import exists, false, func, or_
from sqlalchemy.orm import Session, joinedload

from app.domain.entities import Template, TemplateColumn, TemplateColumnRule
from app.infrastructure.repositories.template_column_repository import (
    TemplateColumnRepository,
)
from app.infrastructure.models import (
    RuleModel,
    TemplateColumnModel,
    TemplateModel,
    TemplateUserAccessModel,
    template_column_rule_table,
)
from app.utils import ensure_app_naive_datetime, now_in_app_timezone


class TemplateRepository:
    """Provide CRUD operations for templates."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        creator_id: int | None = None,
        user_id: int | None = None,
        statuses: Sequence[str] | None = None,
    ) -> Sequence[Template]:
        query = (
            self.session.query(TemplateModel)
            .options(
                joinedload(TemplateModel.columns).joinedload(
                    TemplateColumnModel.rules
                )
            )
            .filter(TemplateModel.deleted == false())
        )
        if creator_id is not None:
            query = query.filter(TemplateModel.created_by == creator_id)
        if user_id is not None:
            self._expire_outdated_accesses(user_id=user_id)
            now = ensure_app_naive_datetime(now_in_app_timezone())
            access_exists = (
                self.session.query(TemplateUserAccessModel.id)
                .filter(TemplateUserAccessModel.template_id == TemplateModel.id)
                .filter(TemplateUserAccessModel.user_id == user_id)
                .filter(TemplateUserAccessModel.revoked_at.is_(None))
                .filter(TemplateUserAccessModel.start_date <= now)
                .filter(
                    or_(
                        TemplateUserAccessModel.end_date.is_(None),
                        TemplateUserAccessModel.end_date >= now,
                    )
                )
                .exists()
            )
            query = query.filter(access_exists)
        if statuses:
            query = query.filter(TemplateModel.status.in_(tuple(statuses)))
        query = query.order_by(TemplateModel.created_at.desc(), TemplateModel.id.desc())
        if skip:
            query = query.offset(skip)
        if limit is not None:
            query = query.limit(limit)
        return [self._to_entity(model) for model in query.all()]

    def _expire_outdated_accesses(self, *, user_id: int) -> None:
        now = ensure_app_naive_datetime(now_in_app_timezone())
        expired_accesses = (
            self.session.query(TemplateUserAccessModel)
            .filter(TemplateUserAccessModel.user_id == user_id)
            .filter(TemplateUserAccessModel.revoked_at.is_(None))
            .filter(TemplateUserAccessModel.end_date.is_not(None))
            .filter(TemplateUserAccessModel.end_date < now)
            .all()
        )
        if not expired_accesses:
            return

        for access in expired_accesses:
            access.revoked_at = now
            access.updated_at = now
            self.session.add(access)
        self.session.commit()

    def get(self, template_id: int) -> Template | None:
        model = self._get_model(id=template_id)
        return self._to_entity(model) if model else None

    def get_by_table_name(self, table_name: str) -> Template | None:
        model = self._get_model(table_name=table_name)
        return self._to_entity(model) if model else None

    def get_by_name(self, name: str, *, created_by: int | None = None) -> Template | None:
        query = (
            self.session.query(TemplateModel)
            .options(
                joinedload(TemplateModel.columns).joinedload(
                    TemplateColumnModel.rules
                )
            )
            .filter(TemplateModel.deleted == false())
        )
        if created_by is None:
            query = query.filter(TemplateModel.created_by.is_(None))
        else:
            query = query.filter(TemplateModel.created_by == created_by)
        normalized_name = name.strip().lower()
        model = (
            query.filter(func.lower(TemplateModel.name) == normalized_name)
            .first()
        )
        return self._to_entity(model) if model else None

    def list_by_creator(self, creator_id: int) -> Sequence[Template]:
        query = (
            self.session.query(TemplateModel)
            .options(
                joinedload(TemplateModel.columns).joinedload(
                    TemplateColumnModel.rules
                )
            )
            .filter(TemplateModel.deleted == false())
            .filter(TemplateModel.created_by == creator_id)
            .order_by(TemplateModel.created_at.desc())
        )
        return [self._to_entity(model) for model in query.all()]

    def create(self, template: Template) -> Template:
        model = TemplateModel()
        self._apply_entity_to_model(model, template, include_creation_fields=True)
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        if not self._relationship_loaded(model):
            self.session.refresh(model, attribute_names=["columns"])
        return self._to_entity(model)

    def update(self, template: Template) -> Template:
        model = self._get_model(id=template.id)
        if not model:
            msg = f"Template with id {template.id} not found"
            raise ValueError(msg)
        self._apply_entity_to_model(model, template, include_creation_fields=False)
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        if not self._relationship_loaded(model):
            self.session.refresh(model, attribute_names=["columns"])
        return self._to_entity(model)

    def delete(self, template_id: int, *, deleted_by: int | None = None) -> None:
        model = self._get_model(id=template_id, include_deleted=True)
        if not model:
            msg = f"Template with id {template_id} not found"
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

    def _get_model(self, include_deleted: bool = False, **filters) -> TemplateModel | None:
        query = self.session.query(TemplateModel).options(
            joinedload(TemplateModel.columns).joinedload(TemplateColumnModel.rules)
        )
        if not include_deleted:
            query = query.filter(TemplateModel.deleted == false())
        return query.filter_by(**filters).first()

    def get_rule_payloads(self, template_id: int) -> dict[int, Any]:
        rows = (
            self.session.query(
                RuleModel.id,
                RuleModel.rule,
                RuleModel.summary,
                RuleModel.attachment,
            )
            .join(
                template_column_rule_table,
                template_column_rule_table.c.rule_id == RuleModel.id,
            )
            .join(
                TemplateColumnModel,
                template_column_rule_table.c.template_column_id
                == TemplateColumnModel.id,
            )
            .filter(TemplateColumnModel.template_id == template_id)
            .filter(TemplateColumnModel.deleted == false())
            .filter(RuleModel.deleted == false())
            .all()
        )
        payloads: dict[int, Any] = {}
        for rule_id, rule_payload, summary, attachment in rows:
            parsed_summary: Any = summary
            if isinstance(summary, str):
                stripped_summary = summary.strip()
                if not stripped_summary:
                    parsed_summary = None
                else:
                    try:
                        parsed_summary = json.loads(stripped_summary)
                    except json.JSONDecodeError:
                        parsed_summary = summary

            payloads[rule_id] = {
                "rule": rule_payload,
                "summary": parsed_summary,
                "attachment": attachment,
            }
        return payloads

    @staticmethod
    def _to_entity(model: TemplateModel) -> Template:
        columns = sorted(
            (
                TemplateRepository._column_to_entity(col)
                for col in model.columns
                if not col.deleted
            ),
            key=lambda column: (column.id is None, column.id or 0),
        )
        return Template(
            id=model.id,
            user_id=model.user_id,
            name=model.name,
            status=model.status,
            description=model.description,
            table_name=model.table_name,
            created_by=model.created_by,
            created_at=ensure_app_naive_datetime(model.created_at),
            updated_by=model.updated_by,
            updated_at=ensure_app_naive_datetime(model.updated_at),
            is_active=model.is_active,
            deleted=model.deleted,
            deleted_by=model.deleted_by,
            deleted_at=ensure_app_naive_datetime(model.deleted_at),
            columns=columns,
        )

    @staticmethod
    def _column_to_entity(model) -> TemplateColumn:
        headers_map, fallback_headers = TemplateColumnRepository._deserialize_rule_headers(
            model.rule_header
        )
        rules: list[TemplateColumnRule] = []
        for rule_model in model.rules:
            if getattr(rule_model, "deleted", False):
                continue
            header_entry = headers_map.get(rule_model.id)
            headers = (
                header_entry.get("headers")
                if isinstance(header_entry, dict)
                else header_entry
            )
            group = (
                header_entry.get("group")
                if isinstance(header_entry, dict)
                else None
            )
            if headers is None and fallback_headers is not None:
                headers = fallback_headers
            rules.append(
                TemplateColumnRule(id=rule_model.id, headers=headers, group=group)
            )

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

    @staticmethod
    def _apply_entity_to_model(
        model: TemplateModel,
        template: Template,
        *,
        include_creation_fields: bool,
    ) -> None:
        if include_creation_fields:
            model.created_by = template.created_by
            model.created_at = (
                ensure_app_naive_datetime(template.created_at)
                or ensure_app_naive_datetime(now_in_app_timezone())
            )
            model.updated_by = None
            model.updated_at = None
        model.user_id = template.user_id
        model.name = template.name
        model.status = template.status
        model.description = template.description
        model.table_name = template.table_name
        if not include_creation_fields:
            model.updated_by = template.updated_by
            model.updated_at = ensure_app_naive_datetime(template.updated_at)
        model.is_active = template.is_active
        model.deleted = template.deleted
        model.deleted_by = template.deleted_by
        model.deleted_at = ensure_app_naive_datetime(template.deleted_at)

    @staticmethod
    def _relationship_loaded(model: TemplateModel) -> bool:
        try:
            return model.columns is not None
        except Exception:  # pragma: no cover - fallback for unloaded relationship
            return False


__all__ = ["TemplateRepository"]
