"""Rutas para administrar reglas de validación."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import replace
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.application.use_cases.rules import (
    create_rule as create_rule_uc,
    delete_rule as delete_rule_uc,
    get_rule as get_rule_uc,
    list_rules as list_rules_uc,
    list_rules_by_creator as list_rules_by_creator_uc,
    update_rule as update_rule_uc,
)
from app.domain.entities import Rule, User
from app.infrastructure.database import get_db
from app.interfaces.api.dependencies import require_admin
from app.interfaces.api.schemas import (
    RuleByType,
    RuleCreate,
    RuleHeaderResponse,
    RuleRead,
    RuleUpdate,
)

router = APIRouter(prefix="/rules", tags=["rules"])

_ALLOWED_TYPE_LABELS: dict[str, str] = {
    "texto": "Texto",
    "numero": "Número",
    "documento": "Documento",
    "lista": "Lista",
    "lista compleja": "Lista compleja",
    "lista completa": "Lista compleja",
    "telefono": "Teléfono",
    "correo": "Correo",
    "fecha": "Fecha",
    "dependencia": "Dependencia",
    "validacion conjunta": "Validación conjunta",
    "duplicados": "Duplicados",
}

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

_DEPENDENCY_PARAMETER_ALIASES: set[str] = {
    "longitud minima",
    "longitud maxima",
    "valor minimo",
    "valor maximo",
    "numero de decimales",
    "formato",
    "fecha minima",
    "fecha maxima",
    "codigo de pais",
    "lista",
    "lista compleja",
    "campos",
    "columnas",
    "nombre de campos",
}


def _normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    collapsed = re.sub(r"[\s\-_/]+", " ", ascii_text)
    return collapsed.lower().strip()


def _iter_rule_definitions(rule_payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(rule_payload, Mapping):
        return [rule_payload]
    if isinstance(rule_payload, Sequence) and not isinstance(rule_payload, (str, bytes)):
        definitions: list[Mapping[str, Any]] = []
        for entry in rule_payload:
            definitions.extend(_iter_rule_definitions(entry))
        return definitions
    return []


def _ensure_supported_type(type_label: str) -> tuple[str, str]:
    normalized = _normalize_label(type_label)
    canonical = _ALLOWED_TYPE_LABELS.get(normalized)
    if canonical is None:
        msg = "Tipo de dato no soportado."
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
    return normalized, canonical
def _sanitize_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value)


def _deduplicate_headers(headers: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for header in headers:
        normalized = header.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(header)
    return ordered


def _extract_header_entries(raw_headers: Any) -> list[str]:
    if isinstance(raw_headers, str):
        candidate = raw_headers.strip()
        return [candidate] if candidate else []
    if isinstance(raw_headers, Sequence) and not isinstance(raw_headers, (str, bytes)):
        candidates: list[str] = []
        for entry in raw_headers:
            if not isinstance(entry, str):
                continue
            candidate = entry.strip()
            if candidate:
                candidates.append(candidate)
        return candidates
    return []


def _extract_dependency_specifics(rule_block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    def _iter_specifics(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        for key, value in candidate.items():
            if isinstance(key, str) and _normalize_label(key) == "reglas especifica":
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


def _collect_specific_leaf_headers(
    rule_block: Mapping[str, Any] | Sequence[Any],
) -> list[str]:
    if not isinstance(rule_block, (Mapping, Sequence)) or isinstance(
        rule_block, (str, bytes)
    ):
        return []

    specifics: list[Mapping[str, Any]] = []
    if isinstance(rule_block, Mapping):
        specifics = _extract_dependency_specifics(rule_block)
    else:
        collected: list[Mapping[str, Any]] = []
        for entry in rule_block:
            if isinstance(entry, Mapping):
                collected.extend(_extract_dependency_specifics(entry))
        specifics = collected

    if not specifics:
        return []

    seen: set[str] = set()
    ordered: list[str] = []

    def add_label(label: str) -> None:
        if not isinstance(label, str):
            return
        candidate = label.strip()
        if not candidate:
            return
        normalized = _normalize_label(candidate)
        if normalized in seen:
            return
        seen.add(normalized)
        ordered.append(candidate)

    _collect_leaf_labels(specifics, add_label)
    return ordered


def _filter_headers_to_specific_leaves(
    headers: Sequence[str],
    rule_block: Mapping[str, Any] | Sequence[Any] | None,
) -> list[str]:
    if not isinstance(headers, Sequence) or isinstance(headers, (str, bytes)):
        if isinstance(headers, Sequence):
            return list(headers)
        return []

    if not isinstance(rule_block, (Mapping, Sequence)) or isinstance(
        rule_block, (str, bytes)
    ):
        return list(headers)

    leaf_headers = _collect_specific_leaf_headers(rule_block)
    if not leaf_headers:
        return list(headers)

    allowed = {_normalize_label(entry) for entry in leaf_headers}
    filtered: list[str] = []
    seen: set[str] = set()
    for header in headers:
        if not isinstance(header, str):
            continue
        candidate = header.strip()
        if not candidate:
            continue
        normalized = _normalize_label(candidate)
        if normalized not in allowed or normalized in seen:
            continue
        seen.add(normalized)
        filtered.append(candidate)

    return filtered if filtered else leaf_headers


def _is_leaf_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            return True
        return not any(
            isinstance(entry, Mapping)
            or (
                isinstance(entry, Sequence)
                and not isinstance(entry, (str, bytes))
            )
            for entry in value
        )
    return True


def _collect_leaf_labels(value: Any, add_label: Callable[[str], None]) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                _collect_leaf_labels(nested, add_label)
                continue

            candidate = key.strip()
            if not candidate:
                _collect_leaf_labels(nested, add_label)
                continue

            normalized_candidate = _normalize_label(candidate)
            if normalized_candidate in _DEPENDENCY_TYPE_ALIASES:
                _collect_leaf_labels(nested, add_label)
                continue

            if _is_leaf_value(nested):
                add_label(candidate)
            else:
                _collect_leaf_labels(nested, add_label)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for entry in value:
            _collect_leaf_labels(entry, add_label)


def _infer_dependency_headers_from_block(
    rule_block: Mapping[str, Any] | Sequence[Any],
) -> list[str]:
    """Collect leaf keys present inside any ``reglas especifica`` block."""

    if isinstance(rule_block, Sequence) and not isinstance(rule_block, (str, bytes)):
        collected: list[str] = []
        seen: set[str] = set()
        for entry in rule_block:
            nested_labels = _infer_dependency_headers_from_block(entry)  # type: ignore[arg-type]
            for label in nested_labels:
                normalized = _normalize_label(label)
                if normalized in seen:
                    continue
                seen.add(normalized)
                collected.append(label)
        return collected

    if not isinstance(rule_block, Mapping):
        return []

    specifics = _extract_dependency_specifics(rule_block)
    if not specifics:
        return []

    seen: set[str] = set()
    ordered: list[str] = []

    def add_label(label: str) -> None:
        if not isinstance(label, str):
            return
        candidate = label.strip()
        if not candidate:
            return
        normalized = _normalize_label(candidate)
        if normalized in seen:
            return
        seen.add(normalized)
        ordered.append(candidate)

    header_rule_context = {"Tipo de dato": "Dependencia", "Regla": rule_block}
    inferred_rule_headers = _infer_header_rule(header_rule_context)
    controlling_label: str | None = None
    if inferred_rule_headers:
        candidate = inferred_rule_headers[0].strip()
        if candidate:
            controlling_label = candidate
            add_label(candidate)

    collected_nested = False
    for entry in specifics:
        if not isinstance(entry, Mapping):
            continue
        for value in entry.values():
            if isinstance(value, Mapping) or (
                isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            ):
                collected_nested = True
                _collect_leaf_labels(value, add_label)

    if not collected_nested:
        _collect_leaf_labels(specifics, add_label)

    if not ordered and controlling_label:
        ordered.append(controlling_label)

    return ordered


def _extract_nested_dependency_field_candidates(value: Any) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def inspect(candidate_value: Any) -> None:
        if isinstance(candidate_value, Mapping):
            for key, nested in candidate_value.items():
                if not isinstance(key, str):
                    inspect(nested)
                    continue

                stripped_key = key.strip()
                if not stripped_key:
                    inspect(nested)
                    continue

                normalized_key = _normalize_label(stripped_key)
                if normalized_key in _DEPENDENCY_TYPE_ALIASES:
                    inspect(nested)
                    continue
                if normalized_key in _DEPENDENCY_PARAMETER_ALIASES:
                    continue
                if normalized_key in seen:
                    continue

                seen.add(normalized_key)
                ordered.append(stripped_key)
        elif isinstance(candidate_value, Sequence) and not isinstance(
            candidate_value, (str, bytes)
        ):
            for item in candidate_value:
                inspect(item)

    inspect(value)
    return ordered


def _infer_header_rule(definition: Mapping[str, Any]) -> list[str]:
    rule_type = _normalize_label(definition.get("Tipo de dato", ""))
    rule_block = definition.get("Regla")
    if not isinstance(rule_block, Mapping):
        return []

    if rule_type in {"lista compleja", "lista completa"}:
        combinations: list[Mapping[str, Any]] = []
        for key, value in rule_block.items():
            if isinstance(key, str) and _normalize_label(key) in {"lista compleja", "lista completa"}:
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    combinations.extend(
                        entry for entry in value if isinstance(entry, Mapping)
                    )
        header_candidates: list[str] = []
        for combination in combinations:
            for header_key in combination.keys():
                if isinstance(header_key, str):
                    candidate = header_key.strip()
                    if candidate:
                        header_candidates.append(candidate)
        return _deduplicate_headers(header_candidates)

    if rule_type == "dependencia":
        specifics = _extract_dependency_specifics(rule_block)
        if not specifics:
            return []

        dependent_label: str | None = None
        header_candidates: list[str] = []
        seen_normalized: set[str] = set()

        for entry in specifics:
            for key, value in entry.items():
                if not isinstance(key, str):
                    continue
                normalized_key = _normalize_label(key)
                stripped_key = key.strip()
                if not stripped_key:
                    continue
                if normalized_key in _DEPENDENCY_TYPE_ALIASES:
                    nested_candidates = _extract_nested_dependency_field_candidates(value)
                    for nested_candidate in nested_candidates:
                        normalized_nested = _normalize_label(nested_candidate)
                        if normalized_nested in seen_normalized:
                            continue
                        header_candidates.append(nested_candidate)
                        seen_normalized.add(normalized_nested)
                    continue
                if dependent_label is None:
                    dependent_label = stripped_key
                    seen_normalized.add(normalized_key)
                elif _normalize_label(dependent_label) == normalized_key:
                    continue

        if dependent_label:
            normalized_required = {_normalize_label(item) for item in header_candidates}
            if _normalize_label(dependent_label) not in normalized_required:
                header_candidates.insert(0, dependent_label)
            elif header_candidates and _normalize_label(header_candidates[0]) != _normalize_label(
                dependent_label
            ):
                header_candidates.insert(0, dependent_label)

        return _deduplicate_headers(header_candidates)

    if rule_type == "validacion conjunta":
        return _deduplicate_headers(
            _extract_header_entries(rule_block.get("Nombre de campos"))
        )

    if rule_type == "duplicados":
        for candidate_key in ("Campos", "Columnas", "Fields", "fields"):
            headers = _extract_header_entries(rule_block.get(candidate_key))
            if headers:
                return _deduplicate_headers(headers)
        return []

    return []


def _extract_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def _sanitize_rule_payload(rule_payload: Any) -> Any:
    if isinstance(rule_payload, Mapping):
        return _sanitize_rule_definition(rule_payload)
    if isinstance(rule_payload, Sequence) and not isinstance(rule_payload, (str, bytes)):
        return [_sanitize_rule_payload(entry) for entry in rule_payload]
    return rule_payload


def _sanitize_rule_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    sanitized_definition = dict(definition)

    rule_block_raw = definition.get("Regla")
    sanitized_rule_block = _sanitize_rule_payload(rule_block_raw)
    if rule_block_raw is not None:
        sanitized_definition["Regla"] = sanitized_rule_block

    header_entries = _deduplicate_headers(
        _extract_header_entries(definition.get("Header"))
    )

    if isinstance(sanitized_rule_block, (Mapping, Sequence)) and not isinstance(
        sanitized_rule_block, (str, bytes)
    ):
        inferred_specific_headers = _infer_dependency_headers_from_block(
            sanitized_rule_block
        )
        if inferred_specific_headers:
            header_entries = _deduplicate_headers(inferred_specific_headers)
        header_entries = _filter_headers_to_specific_leaves(
            header_entries, sanitized_rule_block
        )

    if "Header" in sanitized_definition or header_entries:
        sanitized_definition["Header"] = header_entries

    header_rule_entries = _deduplicate_headers(
        _extract_header_entries(definition.get("Header rule"))
    )
    if "Header rule" in sanitized_definition or header_rule_entries:
        sanitized_definition["Header rule"] = header_rule_entries

    return sanitized_definition


def _normalize_persisted_summary(raw_summary: Any) -> dict[str, Any] | None:
    if raw_summary is None:
        return None

    parsed = raw_summary
    if isinstance(raw_summary, str):
        stripped = raw_summary.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, Mapping):
        return None

    version = parsed.get("version")
    rule_count = parsed.get("rule_count")
    rules = parsed.get("rules")

    if isinstance(version, int) and isinstance(rule_count, int) and isinstance(rules, list):
        return {
            "version": version,
            "rule_count": rule_count,
            "rules": rules,
        }

    if "rule_name" in parsed:
        return {
            "version": 1,
            "rule_count": 1,
            "rules": [dict(parsed)],
        }

    return None


def _to_read_model(rule: Rule) -> RuleRead:
    sanitized_rule = _sanitize_rule_payload(rule.rule)
    summary_payload = _normalize_persisted_summary(rule.summary)
    sanitized_entity = replace(rule, rule=sanitized_rule, summary=summary_payload)
    if hasattr(RuleRead, "model_validate"):
        return RuleRead.model_validate(sanitized_entity)
    return RuleRead.from_orm(sanitized_entity)


@router.post("/", response_model=RuleRead, status_code=status.HTTP_201_CREATED)
def register_rule(
    rule_in: RuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> RuleRead:
    """Crea una nueva regla de validación."""

    try:
        rule = create_rule_uc(
            db,
            rule=rule_in.rule,
            created_by=current_user.id,
            is_active=rule_in.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_read_model(rule)


@router.get("/", response_model=list[RuleRead])
def list_rules(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> list[RuleRead]:
    """Devuelve una lista paginada de reglas de validación."""

    rules = list_rules_uc(
        db, current_user=current_user, skip=skip, limit=limit
    )
    return [_to_read_model(rule) for rule in rules]


@router.get("/by-type/{type_label}", response_model=list[RuleByType])
def list_rules_by_type_endpoint(
    type_label: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> list[RuleByType]:
    """Devuelve las reglas disponibles para el tipo de dato solicitado."""

    normalized_type, canonical_type = _ensure_supported_type(type_label)
    normalized_aliases = {normalized_type}
    if normalized_type in {"lista compleja", "lista completa"}:
        normalized_aliases.update({"lista compleja", "lista completa"})
    max_results = 5
    batch_size = max_results
    skip = 0
    results: list[RuleByType] = []
    while len(results) < max_results:
        rules = list_rules_uc(
            db,
            current_user=current_user,
            skip=skip,
            limit=batch_size,
        )
        if not rules:
            break
        skip += len(rules)

        for rule in rules:
            for definition in _iter_rule_definitions(rule.rule):
                definition_type = _normalize_label(definition.get("Tipo de dato", ""))
                if definition_type not in normalized_aliases:
                    continue

                rule_block = definition.get("Regla")
                inferred_specific_headers: list[str] = []
                if isinstance(rule_block, (Mapping, Sequence)) and not isinstance(
                    rule_block, (str, bytes)
                ):
                    inferred_specific_headers = _infer_dependency_headers_from_block(
                        rule_block
                    )
                if inferred_specific_headers:
                    header_entries = _deduplicate_headers(inferred_specific_headers)
                else:
                    header_entries = _deduplicate_headers(
                        _extract_header_entries(definition.get("Header"))
                    )
                if isinstance(rule_block, (Mapping, Sequence)) and not isinstance(
                    rule_block, (str, bytes)
                ):
                    header_entries = _filter_headers_to_specific_leaves(
                        header_entries, rule_block
                    )
                explicit_header_rule = _deduplicate_headers(
                    _extract_header_entries(definition.get("Header rule"))
                )
                header_rule_entries = (
                    explicit_header_rule
                    if explicit_header_rule
                    else _infer_header_rule(definition)
                )

                payload = {
                    "id": rule.id,
                    "Nombre de la regla": _sanitize_text(
                        definition.get("Nombre de la regla")
                    ),
                    "Tipo de dato": canonical_type,
                    "Campo obligatorio": _extract_bool(
                        definition.get("Campo obligatorio")
                    ),
                    "Mensaje de error": _sanitize_text(
                        definition.get("Mensaje de error")
                    ),
                    "Descripción": _sanitize_text(definition.get("Descripción")),
                    "Ejemplo": definition.get("Ejemplo"),
                    "Header": header_entries,
                    "Header rule": header_rule_entries,
                    "Regla": definition.get("Regla")
                    if isinstance(definition.get("Regla"), (dict, list))
                    else {},
                }

                if hasattr(RuleByType, "model_validate"):
                    results.append(RuleByType.model_validate(payload))
                else:  # pragma: no cover - compatibility path for pydantic v1
                    results.append(RuleByType.parse_obj(payload))

                if len(results) >= max_results:
                    return results

        if len(rules) < batch_size:
            break

    return results


@router.get("/created-by/me", response_model=list[RuleRead])
def list_rules_created_by_admin(
    db: Session = Depends(get_db),
    current_admin: User = Depends(require_admin),
) -> list[RuleRead]:
    """Devuelve todas las reglas creadas por el administrador autenticado."""

    rules = list_rules_by_creator_uc(db, creator_id=current_admin.id)
    return [_to_read_model(rule) for rule in rules]


@router.get("/{rule_id}", response_model=RuleRead)
def read_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> RuleRead:
    """Obtiene la regla identificada por ``rule_id``."""

    try:
        rule = get_rule_uc(db, rule_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _to_read_model(rule)


@router.get("/{rule_id}/headers", response_model=RuleHeaderResponse)
def read_rule_headers(
    rule_id: int,
    tipo: str = Query(..., description="Tipo de dato de la regla"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> RuleHeaderResponse:
    """Obtiene los headers disponibles para reglas complejas."""

    normalized_type, canonical_type = _ensure_supported_type(tipo)
    normalized_aliases = {normalized_type}
    if normalized_type in {"lista compleja", "lista completa"}:
        normalized_aliases.update({"lista compleja", "lista completa"})
    if normalized_type not in {"lista compleja", "lista completa", "dependencia"}:
        msg = "Solo se permiten headers para reglas de lista compleja o dependencia."
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    try:
        rule = get_rule_uc(db, rule_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    matching_definitions = [
        definition
        for definition in _iter_rule_definitions(rule.rule)
        if _normalize_label(definition.get("Tipo de dato", "")) in normalized_aliases
    ]

    if not matching_definitions:
        msg = "La regla no contiene definiciones para el tipo solicitado."
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)

    headers: list[str] = []
    header_rules: list[str] = []
    for definition in matching_definitions:
        rule_block = definition.get("Regla")
        inferred_headers: list[str] = []
        if isinstance(rule_block, (Mapping, Sequence)) and not isinstance(
            rule_block, (str, bytes)
        ):
            inferred_headers = _infer_dependency_headers_from_block(rule_block)

        explicit_headers = _extract_header_entries(definition.get("Header"))
        if inferred_headers:
            explicit_headers = _deduplicate_headers(inferred_headers)
        else:
            explicit_headers = _deduplicate_headers(explicit_headers)
        if isinstance(rule_block, (Mapping, Sequence)) and not isinstance(
            rule_block, (str, bytes)
        ):
            explicit_headers = _filter_headers_to_specific_leaves(
                explicit_headers, rule_block
            )

        headers.extend(explicit_headers)
        explicit = _deduplicate_headers(
            _extract_header_entries(definition.get("Header rule"))
        )
        if explicit:
            header_rules.extend(explicit)
        else:
            header_rules.extend(_infer_header_rule(definition))

    deduplicated = _deduplicate_headers(headers)
    deduplicated_rules = _deduplicate_headers(header_rules)
    payload = {
        "id": rule.id,
        "Tipo de dato": canonical_type,
        "Header": deduplicated,
        "Header rule": deduplicated_rules,
    }

    if hasattr(RuleHeaderResponse, "model_validate"):
        return RuleHeaderResponse.model_validate(payload)
    return RuleHeaderResponse.parse_obj(payload)  # type: ignore[attr-defined]


@router.put("/{rule_id}", response_model=RuleRead)
def update_rule(
    rule_id: int,
    rule_in: RuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> RuleRead:
    """Actualiza una regla de validación existente."""

    if hasattr(rule_in, "model_dump"):
        update_data = rule_in.model_dump(exclude_unset=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        update_data = rule_in.dict(exclude_unset=True)

    rule_body = update_data.get("rule")
    is_active = update_data.get("is_active")

    try:
        rule = update_rule_uc(
            db,
            rule_id=rule_id,
            rule=rule_body,
            is_active=is_active,
            updated_by=current_user.id,
        )
    except ValueError as exc:
        status_code = status.HTTP_400_BAD_REQUEST
        if str(exc) == "Regla no encontrada":
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return _to_read_model(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    """Elimina una regla de validación."""

    try:
        delete_rule_uc(db, rule_id, deleted_by=current_user.id)
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail == "Regla no encontrada":
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
