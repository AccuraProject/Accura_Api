"""Rutas para interactuar con el asistente basado en OpenAI."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.application.use_cases.rules import list_recent_rules as list_recent_rules_uc
from app.domain.entities import Rule, User
from app.infrastructure.database import get_db
from app.infrastructure.openai_client import (
    OffTopicMessageError,
    OpenAIServiceError,
    StructuredChatService,
    _deduplicate_headers,
    _extract_header_entries,
    _generate_dependency_headers,
    _infer_header_rule,
)
from app.interfaces.api.dependencies import (
    get_structured_chat_service,
    require_admin,
)
from app.interfaces.api.schemas import (
    AssistantMessageRequest,
    AssistantMessageResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"])

_DEPENDENCY_TYPE_ALIASES: dict[str, str] = {
    "texto": "Texto",
    "numero": "Número",
    "documento": "Documento",
    "lista": "Lista",
    "lista compleja": "Lista compleja",
    "telefono": "Teléfono",
    "correo": "Correo",
    "fecha": "Fecha",
}


def _normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    collapsed = re.sub(r"[\s\-_]+", " ", ascii_text)
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


def _build_rule_summary(rule_id: int, definition: Mapping[str, Any], type_label: str) -> dict[str, Any]:
    summary: dict[str, Any] = {"id": rule_id, "Tipo de dato": type_label}
    normalized_type = _normalize_label(type_label)
    rule_block: Mapping[str, Any] | None = None
    for key in (
        "Nombre de la regla",
        "Campo obligatorio",
        "Mensaje de error",
        "Descripción",
        "Ejemplo",
    ):
        if key in definition:
            summary[key] = deepcopy(definition[key])
    if "Regla" in definition:
        rule_block = deepcopy(definition["Regla"])

    header_entries = _deduplicate_headers(
        _extract_header_entries(definition.get("Header"))
    )
    if normalized_type == "dependencia":
        inferred_headers = _generate_dependency_headers(definition)
        if inferred_headers:
            header_entries = inferred_headers
    if header_entries:
        summary["Header"] = header_entries
    elif "Header" in definition:
        summary["Header"] = deepcopy(definition["Header"])

    header_rule_entries = _deduplicate_headers(
        _extract_header_entries(definition.get("Header rule"))
    )
    if not header_rule_entries:
        header_rule_entries = _infer_header_rule(definition)
    if not header_entries and header_rule_entries:
        summary["Header"] = list(header_rule_entries)
        header_entries = list(header_rule_entries)
    if not header_rule_entries and header_entries:
        header_rule_entries = list(header_entries)
    if header_rule_entries:
        summary["Header rule"] = header_rule_entries

    if rule_block is not None:
        if normalized_type == "dependencia":
            header_candidates = header_rule_entries or header_entries
            dependent_label = _select_dependency_dependent_label(header_candidates, header_entries)
            if dependent_label:
                rule_block = _remap_dependency_list_specifics(rule_block, dependent_label)
        summary["Regla"] = rule_block

    return summary


def _extract_dependency_variants(definition: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rule_block = definition.get("Regla")
    if not isinstance(rule_block, Mapping):
        return []
    specifics = rule_block.get("reglas especifica")
    if not isinstance(specifics, Sequence):
        return []

    variants: list[tuple[str, dict[str, Any]]] = []
    for entry in specifics:
        if not isinstance(entry, Mapping):
            continue
        dependency_context = {
            key: deepcopy(value)
            for key, value in entry.items()
            if isinstance(key, str) and _normalize_label(key) not in _DEPENDENCY_TYPE_ALIASES
        }
        for key, value in entry.items():
            if not isinstance(key, str):
                continue
            canonical_type = _DEPENDENCY_TYPE_ALIASES.get(_normalize_label(key))
            if canonical_type is None or not isinstance(value, Mapping):
                continue
            payload: dict[str, Any] = {"Regla": deepcopy(value)}
            if dependency_context:
                payload["Dependencia"] = dependency_context
            variants.append((canonical_type, payload))
    return variants


def _select_dependency_dependent_label(
    primary_candidates: Sequence[str] | None, fallback_candidates: Sequence[str] | None
) -> str | None:
    """Return the most likely dependent header label for dependency rules."""

    def iter_candidates(candidates: Sequence[str] | None) -> Sequence[str]:
        if not candidates:
            return []
        return [
            candidate
            for candidate in candidates
            if isinstance(candidate, str) and candidate.strip()
        ]

    for candidates in (primary_candidates, fallback_candidates):
        ordered = iter_candidates(candidates)
        for label in reversed(ordered):
            normalized = _normalize_label(label)
            if normalized and normalized not in _DEPENDENCY_TYPE_ALIASES:
                return label.strip()
    return None


def _remap_dependency_list_specifics(
    rule_block: Mapping[str, Any], dependent_label: str
) -> Mapping[str, Any]:
    """Replace list-based dependency descriptors with the referenced header label."""

    specifics = rule_block.get("reglas especifica")
    if not isinstance(specifics, Sequence):
        return rule_block

    normalized_dependent = _normalize_label(dependent_label)
    remapped_specifics: list[Any] = []
    changed = False

    for entry in specifics:
        if not isinstance(entry, Mapping):
            remapped_specifics.append(deepcopy(entry))
            continue

        normalized_keys = {
            _normalize_label(key): key for key in entry.keys() if isinstance(key, str)
        }
        if normalized_dependent in normalized_keys:
            remapped_specifics.append(deepcopy(entry))
            continue

        entry_changed = False
        transformed_entry: dict[str, Any] = {}
        list_payload: Sequence[Any] | None = None

        for key, value in entry.items():
            if not isinstance(key, str):
                continue

            normalized_key = _normalize_label(key)
            if normalized_key == "lista" and isinstance(value, Mapping):
                allowed_values = value.get("Lista")
                if isinstance(allowed_values, Sequence) and not isinstance(
                    allowed_values, (str, bytes)
                ):
                    list_payload = allowed_values
                    entry_changed = True
                    changed = True
                    break

        if list_payload is not None:
            transformed_entry[dependent_label] = deepcopy(list(list_payload))

        for key, value in entry.items():
            if not isinstance(key, str):
                transformed_entry[key] = deepcopy(value)
                continue

            normalized_key = _normalize_label(key)
            if (
                list_payload is not None
                and normalized_key == normalized_dependent
            ):
                # Preserve the synthesized dependent label payload without overwriting it
                # with the original structure.
                continue
            if list_payload is not None and normalized_key in _DEPENDENCY_TYPE_ALIASES:
                # Skip redundant alias descriptors once the list payload is mapped to the
                # dependent header label.
                continue

            transformed_entry[key] = deepcopy(value)

        if entry_changed:
            remapped_specifics.append(transformed_entry)
        else:
            remapped_specifics.append(deepcopy(entry))

    if not changed:
        return rule_block

    updated_block = dict(rule_block)
    updated_block["reglas especifica"] = remapped_specifics
    return updated_block


def _extract_dependency_header_from_specific(rule_block: Mapping[str, Any]) -> list[str]:
    """Derive header labels from the first dependency-specific block."""

    specifics = rule_block.get("reglas especifica")
    if not isinstance(specifics, Sequence) or not specifics:
        return []

    first_entry = specifics[0]
    if not isinstance(first_entry, Mapping):
        return []

    headers: list[str] = []
    for key, value in first_entry.items():
        if not isinstance(key, str) or not key.strip():
            continue

        if isinstance(value, Mapping):
            for nested_key in value.keys():
                if isinstance(nested_key, str) and nested_key.strip():
                    headers.append(nested_key.strip())
            continue

        headers.append(key.strip())

    return _deduplicate_headers(headers)


def _sanitize_dependency_header(raw_response: Any) -> Any:
    """Replace dependency headers with the labels inferred from specifics."""

    if not isinstance(raw_response, Mapping):
        return raw_response

    tipo_de_dato = raw_response.get("Tipo de dato") or raw_response.get("tipo_de_dato")
    if not isinstance(tipo_de_dato, str) or _normalize_label(tipo_de_dato) != "dependencia":
        return raw_response

    rule_block = raw_response.get("Regla") or raw_response.get("regla")
    if not isinstance(rule_block, Mapping):
        return raw_response

    inferred_headers = _extract_dependency_header_from_specific(rule_block)
    if not inferred_headers:
        return raw_response

    sanitized = dict(raw_response)
    sanitized["Header"] = inferred_headers
    return sanitized


def _build_rules_catalog(rules: Sequence[Rule]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for rule in rules:
        for definition in _iter_rule_definitions(rule.rule):
            if not isinstance(definition, Mapping):
                continue
            type_label = definition.get("Tipo de dato")
            if not isinstance(type_label, str):
                continue

            summary = _build_rule_summary(rule.id, definition, type_label)
            grouped[type_label].append(summary)

            if _normalize_label(type_label) == "dependencia":
                for subtype, payload in _extract_dependency_variants(definition):
                    variant_summary = _build_rule_summary(rule.id, definition, subtype)
                    variant_summary["Regla"] = payload["Regla"]
                    if "Dependencia" in payload:
                        variant_summary["Dependencia"] = payload["Dependencia"]
                    variant_summary["Tipo de dato original"] = type_label
                    grouped[subtype].append(variant_summary)

    catalog = [
        {"Tipo de dato": type_label, "Reglas": entries}
        for type_label, entries in sorted(grouped.items(), key=lambda item: item[0])
    ]
    return catalog


def _merge_rule_sequences(*batches: Sequence[Rule]) -> list[Rule]:
    """Merge rule sequences preserving order and avoiding duplicates by id."""

    merged: list[Rule] = []
    seen_ids: set[int] = set()

    for batch in batches:
        for rule in batch:
            identifier = rule.id
            if isinstance(identifier, int):
                if identifier in seen_ids:
                    continue
                seen_ids.add(identifier)
            merged.append(rule)

    return merged


@router.post("/analyze", response_model=AssistantMessageResponse)
def analyze_message(
    payload: AssistantMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    assistant: StructuredChatService = Depends(get_structured_chat_service),
) -> AssistantMessageResponse:
    """Genera una respuesta estructurada que indica cómo atender el mensaje del usuario."""

    try:
        recent_rules = list_recent_rules_uc(
            db, current_user=current_user, limit=5
        )
        list_rules = list_recent_rules_uc(
            db,
            current_user=current_user,
            limit=10,
            rule_types=("Lista", "Lista compleja"),
        )
        combined_rules = _merge_rule_sequences(recent_rules, list_rules)
        serialized_rules = _build_rules_catalog(combined_rules)
        raw_response = assistant.generate_structured_response(
            payload.message,
            recent_rules=serialized_rules or None,
        )
        logger.debug("Respuesta sin validar del asistente: %s", raw_response)
        raw_response = _sanitize_dependency_header(raw_response)
    except OffTopicMessageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except OpenAIServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    try:
        if hasattr(AssistantMessageResponse, "model_validate"):
            return AssistantMessageResponse.model_validate(raw_response)
        return AssistantMessageResponse.parse_obj(raw_response)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - defensive against schema drift
        logger.exception(
            "Error validando la respuesta estructurada del asistente. Respuesta cruda: %s",
            raw_response,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="La respuesta recibida no coincide con el esquema esperado.",
        ) from exc
