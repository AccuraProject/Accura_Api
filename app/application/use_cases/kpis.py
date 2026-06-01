"""Use case for computing KPI metrics for administrative dashboards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, false, func, or_, true
from sqlalchemy.orm import Session

from app.domain.entities import (
    LOAD_STATUS_VALIDATED_SUCCESS,
    LOAD_STATUS_VALIDATED_WITH_ERRORS,
)
from app.infrastructure.models import (
    LoadModel,
    RuleModel,
    TemplateModel,
    TemplateUserAccessModel,
    UserModel,
)
from app.utils import ensure_app_timezone, now_in_app_timezone


@dataclass
class MonthlyComparison:
    """Represents a metric comparing the current month against the previous one."""

    current_month: int
    previous_month: int


@dataclass
class TemplatePublicationSummary:
    """Summary of published vs unpublished templates."""

    total: int
    published: int
    unpublished: int
    active: int


@dataclass
class RuleSummary:
    """Aggregated metrics describing validation rules."""

    total: int
    active: int
    assigned: int


@dataclass
class HistorySnapshot:
    """Historical aggregated metrics used in dashboards."""

    total_loads: int
    active_users: int
    processed_rows: int


@dataclass
class ValidationEffectiveness:
    """Aggregated information about validation outcomes for the current month."""

    successful: int
    total: int
    effectiveness_percentage: float


@dataclass
class KPIReport:
    """Aggregate view of KPI metrics used in dashboards."""

    active_users: MonthlyComparison
    templates: TemplatePublicationSummary
    loads: MonthlyComparison
    validations: ValidationEffectiveness
    rules: RuleSummary
    history: HistorySnapshot


@dataclass
class ClientKPIReport:
    """Aggregated KPI metrics tailored for end-user dashboards."""

    available_templates: int
    current_month_loads: int
    success_rate: float
    total_loads: int
    successful_loads: int
    successful_rows: int


def _month_boundaries(reference: datetime) -> tuple[datetime, datetime]:
    """Return the start of the reference month and the start of the following month."""

    start = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return start, next_month


def _previous_month_start(current_start: datetime) -> datetime:
    """Return the start datetime of the month preceding ``current_start``."""

    if current_start.month == 1:
        return current_start.replace(year=current_start.year - 1, month=12)
    return current_start.replace(month=current_start.month - 1)


def get_kpis(
    session: Session,
    *,
    reference: datetime | None = None,
    admin_user_id: int | None = None,
) -> KPIReport:
    """Compute KPI metrics required by the administrative dashboard."""

    now = ensure_app_timezone(reference) or now_in_app_timezone()
    current_start, next_month_start = _month_boundaries(now)
    previous_start = _previous_month_start(current_start)
    previous_end = current_start
    completed_statuses = (
        LOAD_STATUS_VALIDATED_SUCCESS,
        LOAD_STATUS_VALIDATED_WITH_ERRORS,
    )

    def _active_users_count(start: datetime, end: datetime) -> int:
        filters = [
            LoadModel.user_id.isnot(None),
            LoadModel.created_at >= start,
            LoadModel.created_at < end,
            UserModel.deleted == false(),
            UserModel.is_active == true(),
        ]
        if admin_user_id is not None:
            filters.extend(
                [
                    UserModel.created_by == admin_user_id,
                    UserModel.id != admin_user_id,
                ]
            )

        return (
            session.query(func.count(func.distinct(LoadModel.user_id)))
            .join(UserModel, LoadModel.user_id == UserModel.id)
            .join(TemplateModel, LoadModel.template_id == TemplateModel.id)
            .filter(and_(*filters))
            .filter(TemplateModel.deleted == false())
            .filter(
                TemplateModel.created_by == admin_user_id
                if admin_user_id is not None
                else true()
            )
            .scalar()
        ) or 0

    active_users_current = _active_users_count(current_start, next_month_start)
    active_users_previous = _active_users_count(previous_start, previous_end)

    template_filters = [TemplateModel.deleted == false()]
    if admin_user_id is not None:
        template_filters.append(TemplateModel.created_by == admin_user_id)

    total_templates = (
        session.query(func.count(TemplateModel.id))
        .filter(and_(*template_filters))
        .scalar()
    ) or 0

    published_templates = (
        session.query(func.count(TemplateModel.id))
        .filter(
            and_(
                TemplateModel.status == "published",
                *template_filters,
            )
        )
        .scalar()
    ) or 0

    active_templates = (
        session.query(func.count(TemplateModel.id))
        .filter(and_(TemplateModel.is_active == true(), *template_filters))
        .scalar()
    ) or 0

    unpublished_templates = max(int(total_templates) - int(published_templates), 0)

    monthly_load_filters = [TemplateModel.deleted == false()]
    if admin_user_id is not None:
        monthly_load_filters.append(TemplateModel.created_by == admin_user_id)

    loads_current = (
        session.query(func.count(LoadModel.id))
        .join(TemplateModel, LoadModel.template_id == TemplateModel.id)
        .filter(
            LoadModel.created_at >= current_start,
            LoadModel.created_at < next_month_start,
            and_(*monthly_load_filters),
        )
        .scalar()
    ) or 0

    loads_previous = (
        session.query(func.count(LoadModel.id))
        .join(TemplateModel, LoadModel.template_id == TemplateModel.id)
        .filter(
            LoadModel.created_at >= previous_start,
            LoadModel.created_at < previous_end,
            and_(*monthly_load_filters),
        )
        .scalar()
    ) or 0

    successful_validations = (
        session.query(
            func.coalesce(func.sum(LoadModel.total_rows - LoadModel.error_rows), 0)
        )
        .join(TemplateModel, LoadModel.template_id == TemplateModel.id)
        .filter(
            LoadModel.status == LOAD_STATUS_VALIDATED_SUCCESS,
            LoadModel.finished_at.isnot(None),
            LoadModel.finished_at >= current_start,
            LoadModel.finished_at < next_month_start,
            and_(*monthly_load_filters),
        )
        .scalar()
    ) or 0

    total_validations = (
        session.query(func.coalesce(func.sum(LoadModel.total_rows), 0))
        .join(TemplateModel, LoadModel.template_id == TemplateModel.id)
        .filter(
            LoadModel.status.in_(completed_statuses),
            LoadModel.finished_at.isnot(None),
            LoadModel.finished_at >= current_start,
            LoadModel.finished_at < next_month_start,
            and_(*monthly_load_filters),
        )
        .scalar()
    ) or 0

    rule_filters = [RuleModel.deleted == false()]
    if admin_user_id is not None:
        rule_filters.append(RuleModel.created_by == admin_user_id)

    total_rules = (
        session.query(func.count(RuleModel.id)).filter(and_(*rule_filters)).scalar()
    ) or 0

    active_rules = (
        session.query(func.count(RuleModel.id))
        .filter(and_(RuleModel.is_active == true(), *rule_filters))
        .scalar()
    ) or 0

    assigned_rules_query = (
        session.query(func.count(RuleModel.id))
        .filter(
            RuleModel.deleted == false(),
            RuleModel.status == "asignada",
        )
    )
    if admin_user_id is not None:
        assigned_rules_query = assigned_rules_query.filter(
            RuleModel.created_by == admin_user_id
        )

    assigned_rules = assigned_rules_query.scalar() or 0

    loads_filters = [TemplateModel.deleted == false()]
    if admin_user_id is not None:
        loads_filters.append(TemplateModel.created_by == admin_user_id)

    total_loads = (
        session.query(func.count(LoadModel.id))
        .join(TemplateModel, LoadModel.template_id == TemplateModel.id)
        .filter(and_(*loads_filters))
        .scalar()
    ) or 0

    processed_rows = (
        session.query(
            func.coalesce(func.sum(LoadModel.total_rows - LoadModel.error_rows), 0)
        )
        .join(TemplateModel, LoadModel.template_id == TemplateModel.id)
        .filter(
            LoadModel.finished_at.isnot(None),
            LoadModel.status.in_(completed_statuses),
            and_(*loads_filters),
        )
        .scalar()
    ) or 0

    history_active_users_query = (
        session.query(func.count(func.distinct(LoadModel.user_id)))
        .join(TemplateModel, LoadModel.template_id == TemplateModel.id)
        .join(UserModel, LoadModel.user_id == UserModel.id)
        .filter(
            LoadModel.user_id.isnot(None),
            UserModel.deleted == false(),
            UserModel.is_active == true(),
            and_(*loads_filters),
        )
    )
    if admin_user_id is not None:
        history_active_users_query = history_active_users_query.filter(
            UserModel.created_by == admin_user_id,
            UserModel.id != admin_user_id,
        )
    history_active_users = history_active_users_query.scalar() or 0

    effectiveness = (
        (successful_validations / total_validations) * 100 if total_validations else 0.0
    )

    return KPIReport(
        active_users=MonthlyComparison(
            current_month=int(active_users_current),
            previous_month=int(active_users_previous),
        ),
        templates=TemplatePublicationSummary(
            total=int(total_templates),
            published=int(published_templates),
            unpublished=int(unpublished_templates),
            active=int(active_templates),
        ),
        loads=MonthlyComparison(
            current_month=int(loads_current),
            previous_month=int(loads_previous),
        ),
        validations=ValidationEffectiveness(
            successful=int(successful_validations),
            total=int(total_validations),
            effectiveness_percentage=float(round(effectiveness, 2)),
        ),
        rules=RuleSummary(
            total=int(total_rules),
            active=int(active_rules),
            assigned=int(assigned_rules),
        ),
        history=HistorySnapshot(
            total_loads=int(total_loads),
            active_users=int(history_active_users),
            processed_rows=int(processed_rows),
        ),
    )


def get_client_kpis(
    session: Session,
    *,
    user_id: int,
    reference: datetime | None = None,
) -> ClientKPIReport:
    """Compute KPI metrics for a specific end-user."""

    now = ensure_app_timezone(reference) or now_in_app_timezone()
    current_start, next_month_start = _month_boundaries(now)

    available_templates = (
        session.query(func.count(func.distinct(TemplateModel.id)))
        .join(
            TemplateUserAccessModel,
            TemplateUserAccessModel.template_id == TemplateModel.id,
        )
        .filter(
            TemplateUserAccessModel.user_id == user_id,
            TemplateUserAccessModel.revoked_at.is_(None),
            TemplateUserAccessModel.start_date <= now,
            or_(
                TemplateUserAccessModel.end_date.is_(None),
                TemplateUserAccessModel.end_date >= now,
            ),
            TemplateModel.deleted == false(),
            TemplateModel.is_active == true(),
        )
        .scalar()
        or 0
    )

    monthly_loads = (
        session.query(func.count(LoadModel.id))
        .filter(
            LoadModel.user_id == user_id,
            LoadModel.created_at >= current_start,
            LoadModel.created_at < next_month_start,
        )
        .scalar()
        or 0
    )

    monthly_successful_loads = (
        session.query(func.count(LoadModel.id))
        .filter(
            LoadModel.user_id == user_id,
            LoadModel.created_at >= current_start,
            LoadModel.created_at < next_month_start,
            LoadModel.status == LOAD_STATUS_VALIDATED_SUCCESS,
        )
        .scalar()
        or 0
    )

    success_rate = (
        round((monthly_successful_loads / monthly_loads) * 100, 2)
        if monthly_loads
        else 0.0
    )

    total_loads = (
        session.query(func.count(LoadModel.id))
        .filter(LoadModel.user_id == user_id)
        .scalar()
        or 0
    )

    successful_loads = (
        session.query(func.count(LoadModel.id))
        .filter(
            LoadModel.user_id == user_id,
            LoadModel.status == LOAD_STATUS_VALIDATED_SUCCESS,
        )
        .scalar()
        or 0
    )

    completed_statuses = (
        LOAD_STATUS_VALIDATED_SUCCESS,
        LOAD_STATUS_VALIDATED_WITH_ERRORS,
    )

    monthly_rows_total = (
        session.query(func.coalesce(func.sum(LoadModel.total_rows), 0))
        .filter(
            LoadModel.user_id == user_id,
            LoadModel.finished_at.isnot(None),
            LoadModel.finished_at >= current_start,
            LoadModel.finished_at < next_month_start,
            LoadModel.status.in_(completed_statuses),
        )
        .scalar()
        or 0
    )

    monthly_error_rows = (
        session.query(func.coalesce(func.sum(LoadModel.error_rows), 0))
        .filter(
            LoadModel.user_id == user_id,
            LoadModel.finished_at.isnot(None),
            LoadModel.finished_at >= current_start,
            LoadModel.finished_at < next_month_start,
            LoadModel.status.in_(completed_statuses),
        )
        .scalar()
        or 0
    )

    successful_rows = max(int(monthly_rows_total) - int(monthly_error_rows), 0)

    return ClientKPIReport(
        available_templates=int(available_templates),
        current_month_loads=int(monthly_loads),
        success_rate=float(success_rate),
        total_loads=int(total_loads),
        successful_loads=int(successful_loads),
        successful_rows=int(successful_rows),
    )


__all__ = [
    "KPIReport",
    "ClientKPIReport",
    "MonthlyComparison",
    "TemplatePublicationSummary",
    "ValidationEffectiveness",
    "RuleSummary",
    "HistorySnapshot",
    "get_kpis",
    "get_client_kpis",
]
