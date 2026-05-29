"""Use case for creating template columns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
import unicodedata
from typing import Any

from sqlalchemy.orm import Session

from app.domain.entities import TemplateColumn, TemplateColumnRule
from app.infrastructure.dynamic_tables import ensure_data_type
from app.infrastructure.repositories import (
    RuleRepository,
    TemplateColumnRepository,
    TemplateRepository,
)
from app.utils import now_in_app_timezone

from .naming import derive_column_identifier, normalize_column_display_name
from .validators import ensure_rule_header_dependencies


_ASSIGNMENT_HEADER_RULE_TYPES = {"lista compleja", "lista completa", "dependencia"}
_DEFAULT_COLUMN_DATA_TYPE = "Texto"


@dataclass(frozen=True)
class NewTemplateColumnRuleData:
    """Information linking a column to a rule during creation."""

    id: int
    header_rule: Sequence[str] | str | None = None


@dataclass(frozen=True)
class NewTemplateColumnData:
    """Data required to create a template column."""

    name: str
    description: str | None = None
    rules: Sequence[NewTemplateColumnRuleData] | None = None
    is_active: bool = True


def _ensure_template_is_editable(template_repository: TemplateRepository, template_id: int):
    template = template_repository.get(template_id)
    if template is None:
        raise ValueError("Plantilla no encontrada")

    if template.status == "published":
        raise ValueError("No se pueden modificar las columnas de una plantilla publicada")

    return template


def _build_column(
    *,
    template_id: int,
    payload: NewTemplateColumnData,
    created_by: int | None,
    forbidden_names: set[str],
    forbidden_identifiers: set[str],
    rule_repository: RuleRepository,
) -> TemplateColumn:
    normalized_name = normalize_column_display_name(payload.name)
    identifier = derive_column_identifier(normalized_name)

    normalized_key = normalized_name.lower()
    if normalized_key in forbidden_names or identifier in forbidden_identifiers:
        raise ValueError("Ya existe una columna con ese nombre en la plantilla")

    forbidden_names.add(normalized_key)
    forbidden_identifiers.add(identifier)

    now = now_in_app_timezone()
    assignments, normalized_type = _prepare_rule_assignments(
        rule_repository, payload.rules
    )

    return TemplateColumn(
        id=None,
        template_id=template_id,
        rules=assignments,
        name=normalized_name,
        description=payload.description,
        data_type=normalized_type,
        created_by=created_by,
        created_at=now,
        updated_by=None,
        updated_at=None,
        is_active=payload.is_active,
        deleted=False,
        deleted_by=None,
        deleted_at=None,
    )


def create_template_column(
    session: Session,
    *,
    template_id: int,
    name: str,
    description: str | None = None,
    rules: Sequence[NewTemplateColumnRuleData] | None = None,
    created_by: int | None = None,
) -> TemplateColumn:
    """Create a new column inside a template.

    Raises:
        ValueError: If the template does not exist or is already published.
    """

    column_repository = TemplateColumnRepository(session)
    template_repository = TemplateRepository(session)
    rule_repository = RuleRepository(session)

    _ensure_template_is_editable(template_repository, template_id)

    existing_columns = list(column_repository.list_by_template(template_id))
    forbidden_names = {column.name.lower() for column in existing_columns}
    forbidden_identifiers = {
        derive_column_identifier(column.name) for column in existing_columns
    }
    column = _build_column(
        template_id=template_id,
        payload=NewTemplateColumnData(
            name=name,
            description=description,
            rules=rules,
        ),
        created_by=created_by,
        forbidden_names=forbidden_names,
        forbidden_identifiers=forbidden_identifiers,
        rule_repository=rule_repository,
    )

    prepared_columns = assign_rule_groups(
        columns=[*existing_columns, column],
        rule_repository=rule_repository,
    )

    ensure_rule_header_dependencies(
        columns=prepared_columns,
        rule_repository=rule_repository,
    )

    saved_column = column_repository.create(prepared_columns[-1])

    return saved_column


def create_template_columns(
    session: Session,
    *,
    template_id: int,
    columns: Sequence[NewTemplateColumnData],
    created_by: int | None = None,
) -> list[TemplateColumn]:
    """Create multiple columns for a template in a single operation."""

    if not columns:
        return []

    column_repository = TemplateColumnRepository(session)
    template_repository = TemplateRepository(session)
    rule_repository = RuleRepository(session)

    _ensure_template_is_editable(template_repository, template_id)

    existing_columns = list(column_repository.list_by_template(template_id))
    forbidden_names = {column.name.lower() for column in existing_columns}
    forbidden_identifiers = {
        derive_column_identifier(column.name) for column in existing_columns
    }

    new_columns: list[TemplateColumn] = []
    for payload in columns:
        column = _build_column(
            template_id=template_id,
            payload=payload,
            created_by=created_by,
            forbidden_names=forbidden_names,
            forbidden_identifiers=forbidden_identifiers,
            rule_repository=rule_repository,
        )
        new_columns.append(column)

    prepared_columns = assign_rule_groups(
        columns=[*existing_columns, *new_columns],
        rule_repository=rule_repository,
    )

    ensure_rule_header_dependencies(
        columns=prepared_columns,
        rule_repository=rule_repository,
    )

    return column_repository.create_many(prepared_columns[len(existing_columns) :])


def _prepare_rule_assignments(
    rule_repository: RuleRepository,
    rules: Sequence[NewTemplateColumnRuleData] | None,
) -> tuple[tuple[TemplateColumnRule, ...], str]:
    if not rules:
        return (), _DEFAULT_COLUMN_DATA_TYPE

    normalized_rules: list[TemplateColumnRule] = []
    normalized_type: str | None = None
    fallback_type: str | None = None
    seen_assignments: set[tuple[int, tuple[str, ...] | None]] = set()
    rule_cache: dict[int, Any] = {}

    for assignment in rules:
        rule_id = _normalize_rule_id(assignment.id)
        headers = _normalize_header_values(assignment.header_rule)

        rule = rule_cache.get(rule_id)
        if rule is None:
            rule = rule_repository.get(rule_id)
            if rule is None or not rule.is_active:
                raise ValueError(
                    f"La regla asociada (ID {rule_id}) a la columna no está disponible."
                )
            rule_cache[rule_id] = rule

        rule_type = _extract_rule_type(rule.rule)
        if not rule_type:
            raise ValueError(
                f"La regla asociada (ID {rule_id}) no define un tipo de dato."
            )

        try:
            canonical_type = ensure_data_type(rule_type)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        normalized_label = _normalize_type_label(canonical_type)
        if normalized_label not in _ASSIGNMENT_HEADER_RULE_TYPES:
            headers = None

        normalized_assignment = (rule_id, headers)
        if normalized_assignment in seen_assignments:
            continue
        seen_assignments.add(normalized_assignment)

        if normalized_label in {"lista compleja", "lista completa", "dependencia"}:
            if not headers:
                raise ValueError(
                    f"La regla asociada (ID {rule_id}) requiere definir 'header rule' al asignarla a la columna."
                )

        if fallback_type is None:
            fallback_type = canonical_type

        if normalized_label not in _AUXILIARY_RULE_TYPES and normalized_type is None:
            normalized_type = canonical_type

        normalized_rules.append(TemplateColumnRule(id=rule_id, headers=headers))

    if normalized_type is None:
        normalized_type = fallback_type

    if normalized_type is None:
        return (), _DEFAULT_COLUMN_DATA_TYPE

    return tuple(normalized_rules), normalized_type


def assign_rule_groups(
    *,
    columns: Sequence[TemplateColumn],
    rule_repository: RuleRepository,
) -> list[TemplateColumn]:
    if not columns:
        return []

    grouped_rule_types = {"lista compleja", "lista completa", "dependencia"}
    rule_cache: dict[int, Any] = {}
    grouped_assignments: dict[int, list[tuple[int, int, tuple[str, ...]]]] = {}
    expected_headers_by_rule: dict[int, set[str]] = {}

    for column_index, column in enumerate(columns):
        for rule_index, assignment in enumerate(column.rules):
            if not assignment.headers:
                continue

            rule = rule_cache.get(assignment.id)
            if rule is None:
                rule = rule_repository.get(assignment.id)
                if rule is None or not rule.is_active:
                    continue
                rule_cache[assignment.id] = rule

            normalized_type = _normalize_type_label(_extract_rule_type(rule.rule) or "")
            if normalized_type not in grouped_rule_types:
                continue

            normalized_headers = tuple(
                normalized_header
                for header in assignment.headers
                for normalized_header in [_normalize_type_label(header)]
                if normalized_header
            )
            if not normalized_headers:
                continue

            grouped_assignments.setdefault(assignment.id, []).append(
                (column_index, rule_index, normalized_headers)
            )
            expected_headers_by_rule.setdefault(assignment.id, set()).update(
                normalized_headers
            )

    assignment_groups: dict[tuple[int, int], int] = {}
    for rule_id, occurrences in grouped_assignments.items():
        expected_headers = expected_headers_by_rule.get(rule_id, set())
        if not expected_headers:
            continue

        pending_keys: list[tuple[int, int]] = []
        covered_headers: set[str] = set()
        current_group = 1

        for column_index, rule_index, normalized_headers in occurrences:
            pending_keys.append((column_index, rule_index))
            covered_headers.update(normalized_headers)

            if expected_headers.issubset(covered_headers):
                for key in pending_keys:
                    assignment_groups[key] = current_group
                current_group += 1
                pending_keys = []
                covered_headers = set()

        if pending_keys:
            for key in pending_keys:
                assignment_groups[key] = current_group

    prepared_columns: list[TemplateColumn] = []
    for column_index, column in enumerate(columns):
        updated_rules: list[TemplateColumnRule] = []
        changed = False
        for rule_index, assignment in enumerate(column.rules):
            group = assignment_groups.get((column_index, rule_index))
            if assignment.group != group:
                changed = True
            updated_rules.append(
                TemplateColumnRule(
                    id=assignment.id,
                    headers=assignment.headers,
                    group=group,
                )
            )
        prepared_columns.append(column.replace_rules(updated_rules) if changed else column)

    return prepared_columns


def _normalize_rule_id(value: Any) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Los identificadores de regla deben ser enteros positivos") from exc
    if numeric < 1:
        raise ValueError("Los identificadores de regla deben ser enteros positivos")
    return numeric


def _normalize_header_values(
    header_rule: Sequence[str] | str | None,
) -> tuple[str, ...] | None:
    if header_rule is None:
        return None

    values: list[str] = []
    if isinstance(header_rule, str):
        candidate = header_rule.strip()
        if candidate:
            values.append(candidate)
    elif isinstance(header_rule, Sequence) and not isinstance(header_rule, (str, bytes)):
        for entry in header_rule:
            if not isinstance(entry, str):
                raise ValueError("Los headers de las reglas deben ser texto.")
            candidate = entry.strip()
            if candidate:
                values.append(candidate)
    else:
        raise ValueError("Los headers de las reglas deben ser texto.")

    if not values:
        return None

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(value)

    return tuple(normalized)


def _extract_rule_type(rule_payload: Any) -> str | None:
    for definition in _iter_rule_definitions(rule_payload):
        candidate = definition.get("Tipo de dato")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _iter_rule_definitions(rule_payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(rule_payload, Mapping):
        return [rule_payload]
    if isinstance(rule_payload, list):
        definitions: list[Mapping[str, Any]] = []
        for entry in rule_payload:
            definitions.extend(_iter_rule_definitions(entry))
        return definitions
    return []


def _normalize_type_label(label: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(label))
    ascii_label = "".join(char for char in normalized if not unicodedata.combining(char))
    collapsed = re.sub(r"[\s\-_/]+", " ", ascii_label)
    return collapsed.lower().strip()


_AUXILIARY_RULE_TYPES: set[str] = {"duplicados", "validacion conjunta"}
