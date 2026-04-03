"""Validation helpers for template column dependencies."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from app.domain.entities import TemplateColumn
from app.infrastructure.repositories import RuleRepository

_RULE_TYPES_REQUIRING_HEADER_FIELD = {"lista compleja", "lista completa"}
_RULE_TYPES_REQUIRING_COLUMN_HEADER = {"lista compleja", "lista completa", "dependencia"}
_RULE_TYPES_WITH_REQUIRED_HEADERS = {
    "lista compleja",
    "lista completa",
    "dependencia",
    "validacion conjunta",
    "duplicados",
}
_HEADER_ENFORCEMENT_RULE_TYPES = {
    "lista compleja",
    "lista completa",
    "dependencia",
    "validacion conjunta",
    "duplicados",
}
_DUPLICATE_FIELD_KEYS: tuple[str, ...] = ("Campos", "Columnas", "Fields", "fields")
_NORMALIZATION_STOPWORDS: set[str] = {
    "de",
    "del",
    "la",
    "el",
    "los",
    "las",
    "un",
    "una",
    "uno",
    "y",
    "the",
    "a",
}


@dataclass(frozen=True)
class _ColumnLabel:
    name: str
    normalized: str
    tokens: tuple[str, ...]
    token_set: frozenset[str]


@dataclass(frozen=True)
class _RuleHeaderValidationResult:
    rule_name: str
    required_headers: tuple[str, ...]
    normalized_required_headers: tuple[str, ...]
    column_type: str
    requires_column_header: bool


_DEPENDENCY_TYPE_ALIASES: set[str] = {
    "texto",
    "numero",
    "documento",
    "lista",
    "lista compleja",
    "lista completa",
    "telefono",
    "correo",
    "fecha",
}


def _normalize_type_label(label: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(label))
    ascii_label = "".join(char for char in normalized if not unicodedata.combining(char))
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", ascii_label)
    separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", separated)
    collapsed = re.sub(r"[\s\-_/]+", " ", separated)
    return collapsed.lower().strip()


def _tokenize_label(label: str) -> tuple[str, ...]:
    normalized = _normalize_type_label(label)
    if not normalized:
        return ()
    tokens = [
        token
        for token in normalized.split()
        if token and token not in _NORMALIZATION_STOPWORDS
    ]
    return tuple(tokens)


def _iter_rule_definitions(rule_data: Any) -> list[Mapping[str, Any]]:
    if isinstance(rule_data, Mapping):
        return [rule_data]
    if isinstance(rule_data, list):
        definitions: list[Mapping[str, Any]] = []
        for entry in rule_data:
            definitions.extend(_iter_rule_definitions(entry))
        return definitions
    return []


def _deduplicate_preserving_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = value.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(value)
    return ordered


def _extract_rule_headers(definition: Mapping[str, Any], key: str) -> list[str]:
    raw_value = definition.get(key)
    headers: list[str] = []
    if isinstance(raw_value, str):
        candidate = raw_value.strip()
        if candidate:
            headers.append(candidate)
    elif isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        for entry in raw_value:
            if not isinstance(entry, str):
                continue
            candidate = entry.strip()
            if candidate:
                headers.append(candidate)
    return _deduplicate_preserving_order(headers)


def _extract_dependency_specifics(rule_block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    def _iter_specifics(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        for key, value in candidate.items():
            if isinstance(key, str) and _normalize_type_label(key) == "reglas especifica":
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    return [entry for entry in value if isinstance(entry, Mapping)]
            if isinstance(value, Mapping):
                nested = _iter_specifics(value)
                if nested:
                    return nested
        fallback = candidate.get("reglas especifica")
        if isinstance(fallback, Sequence) and not isinstance(fallback, (str, bytes)):
            return [entry for entry in fallback if isinstance(entry, Mapping)]
        return []

    return _iter_specifics(rule_block)


def _infer_header_rule(definition: Mapping[str, Any]) -> list[str]:
    rule_type = _normalize_type_label(definition.get("Tipo de dato", ""))
    rule_block = definition.get("Regla")
    if not isinstance(rule_block, Mapping):
        return []

    if rule_type in {"lista compleja", "lista completa"}:
        combinations: list[Mapping[str, Any]] = []
        for key, value in rule_block.items():
            if isinstance(key, str) and _normalize_type_label(key) in {"lista compleja", "lista completa"}:
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    combinations.extend(
                        entry for entry in value if isinstance(entry, Mapping)
                    )
        candidates: list[str] = []
        for combination in combinations:
            for header_key in combination.keys():
                if isinstance(header_key, str):
                    stripped = header_key.strip()
                    if stripped:
                        candidates.append(stripped)
        return _deduplicate_preserving_order(candidates)

    if rule_type == "dependencia":
        specifics = _extract_dependency_specifics(rule_block)
        if not specifics:
            return []

        dependent_label: str | None = None
        candidates: list[str] = []
        normalized_seen: set[str] = set()

        for entry in specifics:
            for key, value in entry.items():
                if not isinstance(key, str):
                    continue
                normalized_key = _normalize_type_label(key)
                stripped = key.strip()
                if not stripped:
                    continue
                if normalized_key in _DEPENDENCY_TYPE_ALIASES:
                    if normalized_key not in normalized_seen:
                        candidates.append(stripped)
                        normalized_seen.add(normalized_key)
                    continue
                if dependent_label is None:
                    dependent_label = stripped
                elif _normalize_type_label(dependent_label) == normalized_key:
                    continue

        if dependent_label:
            normalized_required = {
                _normalize_type_label(value) for value in candidates
            }
            dependent_normalized = _normalize_type_label(dependent_label)
            if dependent_normalized not in normalized_required:
                candidates.insert(0, dependent_label)
            elif candidates and _normalize_type_label(candidates[0]) != dependent_normalized:
                candidates.insert(0, dependent_label)

        return _deduplicate_preserving_order(candidates)

    if rule_type == "validacion conjunta":
        return _deduplicate_preserving_order(
            _extract_rule_headers(rule_block, "Nombre de campos")
        )

    if rule_type == "duplicados":
        for candidate_key in _DUPLICATE_FIELD_KEYS:
            headers = _extract_rule_headers(rule_block, candidate_key)
            if headers:
                return _deduplicate_preserving_order(headers)
        return []

    return []


def _build_available_labels(columns: Sequence[TemplateColumn]) -> list[_ColumnLabel]:
    labels: list[_ColumnLabel] = []
    for column in columns:
        normalized = _normalize_type_label(column.name)
        tokens = _tokenize_label(column.name)
        labels.append(
            _ColumnLabel(
                name=column.name,
                normalized=normalized,
                tokens=tokens,
                token_set=frozenset(tokens),
            )
        )
    return labels


def _resolve_rule_name(definition: Mapping[str, Any], column: TemplateColumn) -> str:
    raw_name = definition.get("Nombre de la regla")
    if isinstance(raw_name, str) and raw_name.strip():
        return raw_name.strip()
    return column.name


def _header_matches(header: str, candidates: Sequence[_ColumnLabel]) -> bool:
    normalized = _normalize_type_label(header)
    tokens = _tokenize_label(header)
    token_set = frozenset(tokens)

    for candidate in candidates:
        if normalized and normalized == candidate.normalized:
            return True
        if tokens and tokens == candidate.tokens:
            return True
        if token_set and candidate.token_set:
            if token_set.issubset(candidate.token_set) or candidate.token_set.issubset(token_set):
                return True
    return False


def normalize_rule_header(header: Sequence[str] | None) -> tuple[str, ...] | None:
    """Normalize user provided headers into a tuple of stripped strings."""

    if header is None:
        return None

    normalized: list[str] = []
    for value in header:
        if not isinstance(value, str):
            raise ValueError("Los headers de las reglas deben ser texto.")
        stripped = value.strip()
        if stripped:
            normalized.append(stripped)

    if not normalized:
        return None

    return tuple(normalized)


def _find_best_duplicate_match(
    field: str, candidates: Sequence[_ColumnLabel]
) -> tuple[str | None, float]:
    normalized = _normalize_type_label(field)
    tokens = _tokenize_label(field)
    token_set = frozenset(tokens)

    best_score = 0.0
    best_candidate: _ColumnLabel | None = None

    for candidate in candidates:
        if normalized and normalized == candidate.normalized:
            return candidate.name, 1.0
        if tokens and tokens == candidate.tokens:
            return candidate.name, 0.99

        score = 0.0
        if token_set and candidate.token_set:
            intersection = len(token_set & candidate.token_set)
            if intersection:
                score = intersection / max(len(token_set), len(candidate.token_set))

        if normalized:
            ratio = SequenceMatcher(None, normalized, candidate.normalized).ratio()
            score = max(score, ratio)

        if score > best_score:
            best_score = score
            best_candidate = candidate

    if best_candidate and best_score >= 0.65:
        return best_candidate.name, best_score
    return None, 0.0


def _validate_duplicate_fields(
    definition: Mapping[str, Any],
    column: TemplateColumn,
    labels: Sequence[_ColumnLabel],
) -> None:
    rule_config = definition.get("Regla")
    if not isinstance(rule_config, Mapping):
        raise ValueError(
            f"La regla '{_resolve_rule_name(definition, column)}' debe definir la configuración de duplicados."
        )

    requested_fields: list[str] = []
    for key in _DUPLICATE_FIELD_KEYS:
        raw_fields = rule_config.get(key)
        if raw_fields is None:
            continue
        if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
            raise ValueError(
                f"'{key}' en la regla '{_resolve_rule_name(definition, column)}' debe ser una lista de campos."
            )
        normalized_fields = [
            field.strip()
            for field in raw_fields
            if isinstance(field, str) and field.strip()
        ]
        if not normalized_fields:
            raise ValueError(
                f"'{key}' en la regla '{_resolve_rule_name(definition, column)}' debe incluir al menos un valor."
            )
        requested_fields.extend(normalized_fields)
        break

    if not requested_fields:
        return

    unmatched_fields: list[str] = []
    for field in requested_fields:
        match, score = _find_best_duplicate_match(field, labels)
        if match is None or score <= 0.0:
            unmatched_fields.append(field)

    if unmatched_fields:
        missing_str = ", ".join(sorted(set(unmatched_fields)))
        raise ValueError(
            "La regla '"
            + _resolve_rule_name(definition, column)
            + "' requiere columnas existentes en la plantilla para: "
            + missing_str
        )


def _validate_column_headers_for_rule(
    *,
    column: TemplateColumn,
    header_values: Sequence[str],
    normalized_header_values: Mapping[str, str],
    labels: Sequence[_ColumnLabel],
    rule_payload: Any,
) -> _RuleHeaderValidationResult:
    definitions = _iter_rule_definitions(rule_payload)
    allows_column_header = False
    requires_column_header = False
    skip_header_enforcement = True
    column_type = _normalize_type_label(column.data_type)
    header_rules_by_type: dict[str, list[str]] = {}
    rule_names_by_type: dict[str, str] = {}

    for definition in definitions:
        rule_type = _normalize_type_label(definition.get("Tipo de dato", ""))
        header_rule_values = _extract_rule_headers(definition, "Header rule")
        if not header_rule_values:
            header_rule_values = _infer_header_rule(definition)
        if rule_type in _HEADER_ENFORCEMENT_RULE_TYPES:
            skip_header_enforcement = False
        if header_rule_values:
            allows_column_header = True
            if rule_type in _RULE_TYPES_WITH_REQUIRED_HEADERS:
                requires_column_header = True
                existing = header_rules_by_type.setdefault(rule_type, [])
                existing_normalized = {
                    _normalize_type_label(value) for value in existing
                }
                for entry in header_rule_values:
                    normalized_entry = _normalize_type_label(entry)
                    if not normalized_entry or normalized_entry in existing_normalized:
                        continue
                    existing.append(entry)
                    existing_normalized.add(normalized_entry)
                rule_names_by_type.setdefault(
                    rule_type, _resolve_rule_name(definition, column)
                )

        if rule_type in _RULE_TYPES_REQUIRING_COLUMN_HEADER and not header_rule_values:
            raise ValueError(
                "La regla '"
                + _resolve_rule_name(definition, column)
                + "' debe definir todos sus 'Header rule' para poder asignarse a columnas."
            )

        if rule_type == "duplicados":
            _validate_duplicate_fields(definition, column, labels)
            continue
        if rule_type == "dependencia":
            allows_column_header = True
            requires_column_header = True
            continue
        if rule_type == "validacion conjunta":
            allows_column_header = True
            requires_column_header = True
            continue
        if rule_type not in _RULE_TYPES_REQUIRING_HEADER_FIELD:
            continue

        if rule_type in _RULE_TYPES_REQUIRING_COLUMN_HEADER:
            allows_column_header = True
            requires_column_header = True

        headers = definition.get("Header")
        if not isinstance(headers, list):
            raise ValueError(
                f"La regla '{_resolve_rule_name(definition, column)}' debe incluir un header con los campos relacionados."
            )

        valid_headers = [
            header.strip()
            for header in headers
            if isinstance(header, str) and header.strip()
        ]
        if not valid_headers:
            raise ValueError(
                f"La regla '{_resolve_rule_name(definition, column)}' debe definir encabezados válidos."
            )

        missing = [
            header
            for header in valid_headers
            if not _header_matches(header, labels)
        ]
        if missing:
            missing_str = ", ".join(sorted(set(missing)))
            raise ValueError(
                f"La regla '{_resolve_rule_name(definition, column)}' requiere que la plantilla incluya las columnas {missing_str}."
            )

    if header_values and not allows_column_header:
        raise ValueError(
            f"La columna '{column.name}' no admite 'header rule' para la regla seleccionada."
        )

    if requires_column_header and not header_values:
        raise ValueError(
            "La columna '"
            + column.name
            + "' debe indicar su 'header rule' para la regla '"
            + rule_names_by_type.get(column_type, column.name)
            + "'."
        )

    required_headers = header_rules_by_type.get(column_type, [])
    normalized_required = [
        _normalize_type_label(value) for value in required_headers
    ]

    if required_headers:
        invalid_assignments = [
            original
            for normalized, original in normalized_header_values.items()
            if normalized not in normalized_required
        ]
        if invalid_assignments:
            rule_name = rule_names_by_type.get(column_type, column.name)
            invalid_str = ", ".join(sorted(set(invalid_assignments)))
            raise ValueError(
                "Los 'header rule' configurados para la columna '"
                + column.name
                + "' deben corresponder a los valores permitidos por la regla '"
                + rule_name
                + "'. Valores no permitidos: "
                + invalid_str
                + "."
            )

        missing_columns = [
            header
            for header in required_headers
            if not _header_matches(header, labels)
        ]
        if missing_columns:
            rule_name = rule_names_by_type.get(column_type, column.name)
            missing_str = ", ".join(sorted(set(missing_columns)))
            raise ValueError(
                "La regla '"
                + rule_name
                + "' requiere que la plantilla incluya las columnas relacionadas en sus 'Header rule': "
                + missing_str
                + "."
            )

    if header_values and not skip_header_enforcement:
        missing_headers = [
            header
            for header in header_values
            if not _header_matches(header, labels)
        ]
        if missing_headers:
            missing_str = ", ".join(sorted(set(missing_headers)))
            raise ValueError(
                f"Los 'header rule' configurados para la columna '{column.name}' requieren que la plantilla incluya las columnas {missing_str}."
            )

    rule_name = rule_names_by_type.get(column_type, column.name)
    return _RuleHeaderValidationResult(
        rule_name=rule_name,
        required_headers=tuple(required_headers),
        normalized_required_headers=tuple(normalized_required),
        column_type=column_type,
        requires_column_header=requires_column_header,
    )


def ensure_rule_header_dependencies(
    *,
    columns: Sequence[TemplateColumn],
    rule_repository: RuleRepository,
) -> None:
    """Ensure complex list and dependency rules reference available headers."""

    active_columns = [column for column in columns if column.is_active]
    if not active_columns:
        return

    labels = _build_available_labels(active_columns)
    rule_cache: dict[int, Any] = {}
    validation_results: dict[tuple[int, str], _RuleHeaderValidationResult] = {}
    assigned_headers: dict[tuple[int, str], dict[str, str]] = {}

    for column in active_columns:
        aggregated_headers = normalize_rule_header(column.rule_header)
        if not column.rules:
            if aggregated_headers:
                raise ValueError(
                    f"La columna '{column.name}' no puede definir headers sin una regla asociada."
                )
            continue

        for assignment in column.rules:
            rule_id = assignment.id
            headers = normalize_rule_header(assignment.headers)
            header_values = list(headers) if headers else []
            if len(header_values) > 1:
                raise ValueError(
                    f"La columna '{column.name}' solo puede definir un 'header rule' por cada regla asignada."
                )

            normalized_header_values = {
                _normalize_type_label(value): value for value in header_values
            }

            cached = rule_cache.get(rule_id)
            if cached is None:
                fetched = rule_repository.get(rule_id)
                if fetched is None or not fetched.is_active:
                    raise ValueError(
                        f"La regla asociada (ID {rule_id}) a la columna '{column.name}' no está disponible."
                    )
                rule_cache[rule_id] = fetched
                rule_payload = fetched.rule
            else:
                rule_payload = cached.rule

            result = _validate_column_headers_for_rule(
                column=column,
                header_values=header_values,
                normalized_header_values=normalized_header_values,
                labels=labels,
                rule_payload=rule_payload,
            )

            key = (rule_id, result.column_type)
            stored = validation_results.get(key)
            if stored is None:
                validation_results[key] = result
            else:
                if stored.normalized_required_headers != result.normalized_required_headers:
                    raise ValueError(
                        "Los headers requeridos para la regla '"
                        + stored.rule_name
                        + "' no son consistentes."
                    )
                if stored.requires_column_header != result.requires_column_header:
                    raise ValueError(
                        "La configuración de headers para la regla '"
                        + stored.rule_name
                        + "' es inconsistente entre columnas."
                    )

            if not header_values:
                continue

            expected_headers = result.normalized_required_headers
            assignments = assigned_headers.setdefault(key, {})
            for normalized, original in normalized_header_values.items():
                if expected_headers and normalized not in expected_headers:
                    raise ValueError(
                        "Los 'header rule' configurados para la columna '"
                        + column.name
                        + "' deben corresponder a los valores permitidos por la regla '"
                        + result.rule_name
                        + "'. Valor recibido: "
                        + original
                        + "."
                    )

                previous_column = assignments.get(normalized)
                if previous_column and previous_column != column.name:
                    raise ValueError(
                        "El 'header rule' '"
                        + original
                        + "' de la regla '"
                        + result.rule_name
                        + "' ya fue asignado a la columna '"
                        + previous_column
                        + "'."
                    )
                assignments[normalized] = column.name

    for key, result in validation_results.items():
        if not result.normalized_required_headers:
            continue

        assignments = assigned_headers.get(key, {})
        missing_assignments = [
            header
            for header, normalized in zip(
                result.required_headers, result.normalized_required_headers
            )
            if normalized not in assignments
        ]
        if missing_assignments:
            missing_str = ", ".join(sorted(set(missing_assignments)))
            raise ValueError(
                "La regla '"
                + result.rule_name
                + "' requiere que se asignen todos sus 'header rule'. Faltan: "
                + missing_str
                + "."
            )


__all__ = [
    "ensure_rule_header_dependencies",
    "normalize_rule_header",
    "normalize_rule_ids",
]


def normalize_rule_ids(rule_ids: Sequence[int] | None) -> tuple[int, ...]:
    """Normalize rule identifiers ensuring they are unique positive integers."""

    if not rule_ids:
        return ()

    normalized: list[int] = []
    seen: set[int] = set()
    for value in rule_ids:
        if value is None:
            continue
        if isinstance(value, bool):  # pragma: no cover - defensive branch
            raise ValueError("Los identificadores de regla deben ser enteros positivos")
        try:
            numeric_value = int(value)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive branch
            raise ValueError("Los identificadores de regla deben ser enteros positivos") from exc
        if numeric_value < 1:
            raise ValueError("Los identificadores de regla deben ser enteros positivos")
        if numeric_value not in seen:
            seen.add(numeric_value)
            normalized.append(numeric_value)

    return tuple(normalized)

