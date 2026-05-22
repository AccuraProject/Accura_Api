"""Persistence helpers for template access assignments."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import case, or_
from sqlalchemy.orm import Session

from app.domain.entities import TemplateUserAccess
from app.infrastructure.models import TemplateUserAccessModel
from app.utils import ensure_app_naive_datetime, now_in_app_timezone


class TemplateUserAccessRepository:
    """Provide CRUD operations for template access records."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_by_template(
        self,
        template_id: int,
        *,
        include_inactive: bool = False,
        include_scheduled: bool = False,
    ) -> Sequence[TemplateUserAccess]:
        self._expire_outdated_accesses(template_id=template_id)
        query = self.session.query(TemplateUserAccessModel).filter(
            TemplateUserAccessModel.template_id == template_id,
            TemplateUserAccessModel.revoked_at.is_(None),
        )
        if not include_inactive:
            now = ensure_app_naive_datetime(now_in_app_timezone())
            filters = [
                (
                    TemplateUserAccessModel.end_date.is_(None)
                    | (TemplateUserAccessModel.end_date >= now)
                ),
            ]
            if not include_scheduled:
                filters.append(TemplateUserAccessModel.start_date <= now)
            query = query.filter(*filters)
        query = query.order_by(TemplateUserAccessModel.start_date.desc())
        return [self._to_entity(model) for model in query.all()]

    def list_by_user(
        self,
        user_id: int,
        *,
        include_inactive: bool = False,
        include_scheduled: bool = False,
    ) -> Sequence[TemplateUserAccess]:
        self._expire_outdated_accesses(user_id=user_id)
        query = self.session.query(TemplateUserAccessModel).filter(
            TemplateUserAccessModel.user_id == user_id,
            TemplateUserAccessModel.revoked_at.is_(None),
        )
        if not include_inactive:
            now = ensure_app_naive_datetime(now_in_app_timezone())
            filters = [
                (
                    TemplateUserAccessModel.end_date.is_(None)
                    | (TemplateUserAccessModel.end_date >= now)
                ),
            ]
            if not include_scheduled:
                filters.append(TemplateUserAccessModel.start_date <= now)
            query = query.filter(*filters)
        query = query.order_by(TemplateUserAccessModel.start_date.desc())
        return [self._to_entity(model) for model in query.all()]

    def get(self, access_id: int) -> TemplateUserAccess | None:
        model = self.session.get(TemplateUserAccessModel, access_id)
        return self._to_entity(model) if model else None

    def get_by_template_and_user(
        self,
        *,
        template_id: int,
        user_id: int,
    ) -> TemplateUserAccess | None:
        self._expire_outdated_accesses(template_id=template_id, user_id=user_id)
        now = ensure_app_naive_datetime(now_in_app_timezone())
        model = (
            self.session.query(TemplateUserAccessModel)
            .filter(
                TemplateUserAccessModel.template_id == template_id,
                TemplateUserAccessModel.user_id == user_id,
                TemplateUserAccessModel.revoked_at.is_(None),
            )
            .order_by(
                case(
                    (
                        TemplateUserAccessModel.start_date <= now,
                        case(
                            (
                                or_(
                                    TemplateUserAccessModel.end_date.is_(None),
                                    TemplateUserAccessModel.end_date >= now,
                                ),
                                0,
                            ),
                            else_=2,
                        ),
                    ),
                    else_=1,
                ),
                TemplateUserAccessModel.start_date.asc(),
            )
            .first()
        )
        return self._to_entity(model) if model else None

    def get_overlapping_access(
        self,
        *,
        template_id: int,
        user_id: int,
        start_date: datetime,
        end_date: datetime | None,
        exclude_access_id: int | None = None,
    ) -> TemplateUserAccess | None:
        self._expire_outdated_accesses(template_id=template_id, user_id=user_id)
        query = self.session.query(TemplateUserAccessModel).filter(
            TemplateUserAccessModel.template_id == template_id,
            TemplateUserAccessModel.user_id == user_id,
            TemplateUserAccessModel.revoked_at.is_(None),
            or_(
                TemplateUserAccessModel.end_date.is_(None),
                TemplateUserAccessModel.end_date >= start_date,
            ),
        )
        if end_date is not None:
            query = query.filter(TemplateUserAccessModel.start_date <= end_date)
        if exclude_access_id is not None:
            query = query.filter(TemplateUserAccessModel.id != exclude_access_id)
        model = query.order_by(TemplateUserAccessModel.start_date.desc()).first()
        return self._to_entity(model) if model else None

    def get_active_access(
        self,
        *,
        user_id: int,
        template_id: int,
        reference_time: datetime | None = None,
    ) -> TemplateUserAccess | None:
        self._expire_outdated_accesses(template_id=template_id, user_id=user_id)
        if reference_time is None:
            reference_time = ensure_app_naive_datetime(now_in_app_timezone())
        model = (
            self.session.query(TemplateUserAccessModel)
            .filter(
                TemplateUserAccessModel.user_id == user_id,
                TemplateUserAccessModel.template_id == template_id,
                TemplateUserAccessModel.revoked_at.is_(None),
                TemplateUserAccessModel.start_date <= reference_time,
                (
                    TemplateUserAccessModel.end_date.is_(None)
                    | (TemplateUserAccessModel.end_date >= reference_time)
                ),
            )
            .order_by(TemplateUserAccessModel.start_date.desc())
            .first()
        )
        return self._to_entity(model) if model else None

    def create(self, access: TemplateUserAccess) -> TemplateUserAccess:
        model = TemplateUserAccessModel()
        self._apply_entity_to_model(model, access, include_creation_fields=True)
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        return self._to_entity(model)

    def revoke(
        self,
        *,
        access_id: int,
        revoked_by: int,
        revoked_at: datetime | None = None,
    ) -> TemplateUserAccess:
        model = self.session.get(TemplateUserAccessModel, access_id)
        if model is None:
            msg = f"Template access with id {access_id} not found"
            raise ValueError(msg)
        model.revoked_by = revoked_by
        model.revoked_at = (
            ensure_app_naive_datetime(revoked_at)
            or ensure_app_naive_datetime(now_in_app_timezone())
        )
        model.updated_at = model.revoked_at
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        return self._to_entity(model)

    def update(self, access: TemplateUserAccess) -> TemplateUserAccess:
        model = self.session.get(TemplateUserAccessModel, access.id)
        if model is None:
            msg = f"Template access with id {access.id} not found"
            raise ValueError(msg)
        self._apply_entity_to_model(model, access, include_creation_fields=False)
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        return self._to_entity(model)

    def _expire_outdated_accesses(
        self,
        *,
        template_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        now = ensure_app_naive_datetime(now_in_app_timezone())
        query = self.session.query(TemplateUserAccessModel).filter(
            TemplateUserAccessModel.revoked_at.is_(None),
            TemplateUserAccessModel.end_date.is_not(None),
            TemplateUserAccessModel.end_date < now,
        )
        if template_id is not None:
            query = query.filter(TemplateUserAccessModel.template_id == template_id)
        if user_id is not None:
            query = query.filter(TemplateUserAccessModel.user_id == user_id)

        expired_models = query.all()
        if not expired_models:
            return

        for model in expired_models:
            model.revoked_at = now
            model.updated_at = now
            self.session.add(model)
        self.session.commit()

    @staticmethod
    def _to_entity(model: TemplateUserAccessModel) -> TemplateUserAccess:
        return TemplateUserAccess(
            id=model.id,
            template_id=model.template_id,
            user_id=model.user_id,
            start_date=ensure_app_naive_datetime(model.start_date),
            end_date=ensure_app_naive_datetime(model.end_date),
            revoked_at=ensure_app_naive_datetime(model.revoked_at),
            revoked_by=model.revoked_by,
            created_at=ensure_app_naive_datetime(model.created_at),
            updated_at=ensure_app_naive_datetime(model.updated_at),
        )

    @staticmethod
    def _apply_entity_to_model(
        model: TemplateUserAccessModel,
        access: TemplateUserAccess,
        *,
        include_creation_fields: bool,
    ) -> None:
        model.template_id = access.template_id
        model.user_id = access.user_id
        model.start_date = ensure_app_naive_datetime(access.start_date)
        model.end_date = ensure_app_naive_datetime(access.end_date)
        model.revoked_at = ensure_app_naive_datetime(access.revoked_at)
        model.revoked_by = access.revoked_by
        if include_creation_fields:
            model.created_at = (
                ensure_app_naive_datetime(access.created_at)
                or ensure_app_naive_datetime(now_in_app_timezone())
            )
        model.updated_at = (
            ensure_app_naive_datetime(access.updated_at)
            or ensure_app_naive_datetime(now_in_app_timezone())
        )


__all__ = ["TemplateUserAccessRepository"]
