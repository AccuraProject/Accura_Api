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
from sqlalchemy import false
from sqlalchemy.orm import Session, joinedload

from app.application.use_cases.rules import list_recent_rules as list_recent_rules_uc
from app.domain.entities import Rule, User
from app.infrastructure.database import get_db
from app.infrastructure.models import RuleModel, TemplateColumnModel, TemplateModel
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


_SUPPORTED_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "Texto": ("texto", "cadena", "caracter", "caracteres", "letras", "longitud"),
    "NÃºmero": (
        "numero",
        "nÃºmero",
        "porcentaje",
        "decimal",
        "entero",
        "valor minimo",
        "valor mÃ¡ximo",
        "rango",
        "mayor",
        "menor",
    ),
    "Documento": ("documento", "dni", "ruc", "ce", "pasaporte"),
    "Lista": ("lista", "opciones", "valores permitidos", "catÃ¡logo", "catalogo"),
    "Lista compleja": ("lista compleja", "combinacion", "combinaciÃ³n"),
    "TelÃ©fono": ("telefono", "telÃ©fono", "celular", "codigo de pais", "cÃ³digo de paÃ­s"),
    "Correo": ("correo", "email", "e-mail"),
    "Fecha": ("fecha", "yyyy", "dd/mm", "mm-dd"),
    "Dependencia": ("dependencia", "dependa", "depender", "si ", "cuando "),
    "ValidaciÃ³n conjunta": ("validacion conjunta", "validaciÃ³n conjunta", "coincida con", "coincidir"),
    "Duplicados": ("duplicado", "duplicados", "Ãºnico", "unico", "repetido", "repetidos"),
}

_ACTIONABLE_RULE_MARKERS: tuple[str, ...] = (
    "debe",
    "debera",
    "deberÃ¡",
    "solo",
    "solamente",
    "unicamente",
    "Ãºnicamente",
    "permitido",
    "permitidos",
    "prohibido",
    "prohibidos",
    "distinto",
    "diferente",
    "igual",
    "mayor",
    "menor",
    "entre",
    "minimo",
    "mÃ­nimo",
    "maximo",
    "mÃ¡ximo",
    "longitud",
    "formato",
    "obligatorio",
    "regex",
    "patron",
    "patrÃ³n",
    "dependa",
    "dependencia",
    "duplicado",
    "coincida",
)

_VAGUE_RULE_MARKERS: tuple[str, ...] = (
    "sea correcto",
    "este correcto",
    "estÃ© correcto",
    "no tenga errores",
    "si esta mal",
    "si estÃ¡ mal",
    "si es incorrecto",
    "si es invalido",
    "si es invÃ¡lido",
    "mostrar un mensaje",
    "cualquiera",
    "no valido",
    "no vÃ¡lido",
    "lo que corresponda",
    "segun corresponda",
    "segÃºn corresponda",
)


# ASCII overrides to avoid mojibake in source literals.
_DEPENDENCY_TYPE_ALIASES = {
    "texto": "Texto",
    "numero": "Numero",
    "documento": "Documento",
    "lista": "Lista",
    "lista compleja": "Lista compleja",
    "telefono": "Telefono",
    "correo": "Correo",
    "fecha": "Fecha",
}

_SUPPORTED_TYPE_HINTS = {
    "Texto": ("texto", "cadena", "caracter", "caracteres", "letras", "longitud"),
    "Numero": (
        "numero",
        "porcentaje",
        "decimal",
        "entero",
        "valor minimo",
        "valor maximo",
        "rango",
        "mayor",
        "menor",
    ),
    "Documento": ("documento", "dni", "ruc", "ce", "pasaporte"),
    "Lista": ("lista", "opciones", "valores permitidos", "catalogo"),
    "Lista compleja": ("lista compleja", "combinacion"),
    "Telefono": ("telefono", "celular", "codigo de pais"),
    "Correo": ("correo", "email", "e-mail"),
    "Fecha": ("fecha", "yyyy", "dd/mm", "mm-dd"),
    "Dependencia": ("dependencia", "dependa", "depender", "si ", "cuando "),
    "Validacion conjunta": ("validacion conjunta", "coincida con", "coincidir"),
    "Duplicados": ("duplicado", "duplicados", "unico", "repetido", "repetidos"),
}

_ACTIONABLE_RULE_MARKERS = (
    "debe",
    "debera",
    "solo",
    "solamente",
    "unicamente",
    "permitido",
    "permitidos",
    "prohibido",
    "prohibidos",
    "distinto",
    "diferente",
    "igual",
    "mayor",
    "menor",
    "entre",
    "minimo",
    "maximo",
    "longitud",
    "formato",
    "obligatorio",
    "regex",
    "patron",
    "dependa",
    "dependencia",
    "duplicado",
    "coincida",
)

_VAGUE_RULE_MARKERS = (
    "sea correcto",
    "este correcto",
    "no tenga errores",
    "si esta mal",
    "si es incorrecto",
    "si es invalido",
    "mostrar un mensaje",
    "cualquiera",
    "no valido",
    "lo que corresponda",
    "segun corresponda",
)

_RULE_NAME_PARAPHRASE_TEMPLATES: tuple[str, ...] = (
    "Validación {type} {fields} {focus}",
    "Validación {type} {fields} con {focus}",
    "Validación {type} {fields} por {focus}",
    "Validación {type} {focus} en {fields}",
)

_RULE_NAME_FOCUS_SYNONYMS: dict[str, tuple[str, ...]] = {
    "longitud": ("tamano", "extension"),
    "rango": ("limites", "valor permitido"),
    "formato": ("estructura", "patron"),
    "valores permitidos": ("opciones validas", "lista permitida"),
    "combinaciones permitidas": ("combinaciones validas", "relacion permitida"),
    "unicidad": ("no repetidos", "valor unico"),
    "consistencia": ("coherencia", "relacion esperada"),
    "condicion": ("regla condicional", "criterio dependiente"),
    "longitud condicional": ("tamano condicional", "extension condicional"),
    "rango condicional": ("limites condicionales", "valor condicionado"),
    "formato condicional": ("estructura condicional", "patron condicionado"),
}


def _normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    collapsed = re.sub(r"[\s\-_]+", " ", ascii_text)
    return collapsed.lower().strip()


def _tokenize_label(value: str) -> tuple[str, ...]:
    normalized = _normalize_label(value)
    tokens = [
        token
        for token in re.split(r"\s+", normalized)
        if token and token not in {"de", "del", "la", "el", "los", "las"}
    ]
    return tuple(tokens)


def _labels_match(candidate: str, reference: str) -> bool:
    normalized_candidate = _normalize_label(candidate)
    normalized_reference = _normalize_label(reference)
    if normalized_candidate == normalized_reference:
        return True
    if normalized_candidate in normalized_reference or normalized_reference in normalized_candidate:
        return True

    candidate_tokens = set(_tokenize_label(candidate))
    reference_tokens = set(_tokenize_label(reference))
    if not candidate_tokens or not reference_tokens:
        return False
    return candidate_tokens == reference_tokens or candidate_tokens.issubset(reference_tokens) or reference_tokens.issubset(candidate_tokens)


def _extract_quoted_labels(message: str) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()

    for match in re.finditer(r"[\"'“”‘’]([^\"'“”‘’]{2,80})[\"'“”‘’]", message):
        candidate = match.group(1).strip()
        normalized = _normalize_label(candidate)
        if not candidate or normalized in seen:
            continue
        seen.add(normalized)
        labels.append(candidate)

    return labels


def _extract_dependency_field_candidates(message: str) -> tuple[str | None, str | None]:
    labels = _extract_quoted_labels(message)
    if len(labels) >= 2:
        return labels[0], labels[1]
    return None, None


def _infer_supported_data_types(message: str) -> set[str]:
    normalized = _normalize_label(message)
    inferred_types: set[str] = set()

    for type_label, hints in _SUPPORTED_TYPE_HINTS.items():
        for hint in hints:
            if _normalize_label(hint) in normalized:
                inferred_types.add(type_label)
                break

    if re.search(r"\b\d+(?:[.,]\d+)?\b", normalized):
        inferred_types.add("NÃºmero")

    return inferred_types


def _has_actionable_rule_detail(message: str) -> bool:
    normalized = _normalize_label(message)
    if re.search(r"\b\d+(?:[.,]\d+)?\b", normalized):
        return True

    quoted_labels = _extract_quoted_labels(message)
    if len(quoted_labels) >= 2 and any(
        marker in normalized for marker in ("si ", "cuando ", "debe ", "solo ", "distinto ")
    ):
        return True

    return any(marker in normalized for marker in _ACTIONABLE_RULE_MARKERS)


def _validate_message_semantics(message: str) -> str | None:
    normalized = _normalize_label(message)
    inferred_types = _infer_supported_data_types(message)
    actionable_detail = _has_actionable_rule_detail(message)
    vague_detail = any(marker in normalized for marker in _VAGUE_RULE_MARKERS)

    if vague_detail and not actionable_detail:
        return (
            "El mensaje tiene forma de regla, pero no define una validaciÃ³n concreta. "
            "Indica una condiciÃ³n real, por ejemplo lÃ­mites, formato, valores permitidos o una dependencia clara."
        )

    if not actionable_detail:
        return (
            "El mensaje no describe una validaciÃ³n accionable. "
            "Falta indicar una restricciÃ³n concreta como rango, formato, lista permitida, unicidad o dependencia."
        )

    if not inferred_types:
        return (
            "No se pudo relacionar el mensaje con un tipo de dato soportado. "
            "Especifica mejor si se trata de texto, nÃºmero, documento, lista, fecha, correo, telÃ©fono, dependencia, validaciÃ³n conjunta o duplicados."
        )

    return None


def _validate_dependency_message_coherence(message: str) -> str | None:
    normalized = _normalize_label(message)
    dependency_markers = ("depend", "dependa", "depender", "dependencia")
    if not any(marker in normalized for marker in dependency_markers):
        return None

    dependent_field, source_field = _extract_dependency_field_candidates(message)
    if not dependent_field or not source_field:
        return (
            "El mensaje parece pedir una dependencia, pero no identifica con claridad "
            "dos campos distintos entre comillas para relacionarlos."
        )

    if _normalize_label(dependent_field) == _normalize_label(source_field):
        return (
            "La dependencia debe involucrar dos campos distintos. "
            "El mensaje actual repite el mismo campo."
        )

    logical_markers = (
        " si ",
        " debe ",
        " debera ",
        " deberá ",
        " cuando ",
        " en caso contrario ",
        " mostrar ",
        " mensaje ",
        " unicamente ",
        " únicamente ",
        " distinto ",
        " diferente ",
    )
    if not any(marker in f" {normalized} " for marker in logical_markers):
        return (
            "El mensaje identifica campos, pero no describe una condición o consecuencia "
            "lo bastante clara para construir la dependencia."
        )

    return None


def _iter_scalar_values(value: Any) -> list[str]:
    collected: list[str] = []

    if isinstance(value, str):
        candidate = value.strip()
        if candidate:
            collected.append(candidate)
        return collected

    if isinstance(value, Mapping):
        for nested in value.values():
            collected.extend(_iter_scalar_values(nested))
        return collected

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if isinstance(item, Mapping):
                continue
            collected.extend(_iter_scalar_values(item))

    return collected


def _extract_list_values_for_header(
    definition: Mapping[str, Any], target_header: str
) -> list[str]:
    normalized_target = _normalize_label(target_header)
    collected: list[str] = []
    seen: set[str] = set()

    rule_type = definition.get("Tipo de dato")
    rule_block = definition.get("Regla")

    if (
        isinstance(rule_type, str)
        and _normalize_label(rule_type) == "lista"
        and isinstance(rule_block, Mapping)
    ):
        header_entries = _deduplicate_headers(_extract_header_entries(definition.get("Header rule")))
        if not header_entries:
            header_entries = _deduplicate_headers(_extract_header_entries(definition.get("Header")))
        if header_entries and _normalize_label(header_entries[0]) == normalized_target:
            for value in _iter_scalar_values(rule_block.get("Lista")):
                normalized_value = _normalize_label(value)
                if normalized_value in seen:
                    continue
                seen.add(normalized_value)
                collected.append(value)

    if (
        isinstance(rule_type, str)
        and _normalize_label(rule_type) == "dependencia"
        and isinstance(rule_block, Mapping)
    ):
        specifics = rule_block.get("reglas especifica")
        if isinstance(specifics, Sequence) and not isinstance(specifics, (str, bytes)):
            for entry in specifics:
                if not isinstance(entry, Mapping):
                    continue
                for key, value in entry.items():
                    if not isinstance(key, str) or _normalize_label(key) != normalized_target:
                        continue
                    for item in _iter_scalar_values(value):
                        normalized_value = _normalize_label(item)
                        if normalized_value in seen:
                            continue
                        seen.add(normalized_value)
                        collected.append(item)

    return collected


def _extract_dependency_values_from_rule_definition(
    definition: Mapping[str, Any], target_header: str
) -> list[str]:
    rule_type = definition.get("Tipo de dato")
    if not isinstance(rule_type, str) or _normalize_label(rule_type) != "dependencia":
        return []

    header_rule_entries = _deduplicate_headers(_extract_header_entries(definition.get("Header rule")))
    if not any(_labels_match(target_header, entry) for entry in header_rule_entries):
        return []

    rule_block = definition.get("Regla")
    if not isinstance(rule_block, Mapping):
        return []

    specifics = rule_block.get("reglas especifica")
    if not isinstance(specifics, Sequence) or isinstance(specifics, (str, bytes)):
        return []

    collected: list[str] = []
    seen: set[str] = set()

    for entry in specifics:
        if not isinstance(entry, Mapping):
            continue
        for key, value in entry.items():
            if not isinstance(key, str) or not _labels_match(target_header, key):
                continue
            for item in _iter_scalar_values(value):
                normalized_item = _normalize_label(item)
                if normalized_item in seen:
                    continue
                seen.add(normalized_item)
                collected.append(item)

    return collected


def _build_dependency_rule_context(
    db: Session,
    creator_scope: int,
    candidate_labels: Sequence[str],
) -> list[dict[str, Any]]:
    if not candidate_labels:
        return []

    matched_context: list[dict[str, Any]] = []
    seen_headers: set[str] = set()

    rule_models = (
        db.query(RuleModel)
        .filter(RuleModel.deleted == false())
        .filter(RuleModel.created_by == creator_scope)
        .order_by(RuleModel.id.desc())
        .all()
    )

    for rule_model in rule_models:
        for definition in _iter_rule_definitions(rule_model.rule):
            if not isinstance(definition, Mapping):
                continue
            rule_type = definition.get("Tipo de dato")
            if not isinstance(rule_type, str) or _normalize_label(rule_type) != "dependencia":
                continue

            header_rule_entries = _deduplicate_headers(
                _extract_header_entries(definition.get("Header rule"))
            )
            for candidate_label in candidate_labels:
                matched_header = next(
                    (entry for entry in header_rule_entries if _labels_match(candidate_label, entry)),
                    None,
                )
                if not matched_header:
                    continue

                normalized_header = _normalize_label(matched_header)
                if normalized_header in seen_headers:
                    continue

                values = _extract_dependency_values_from_rule_definition(definition, matched_header)
                payload: dict[str, Any] = {
                    "nombre": matched_header,
                    "origen": "rule.header_rule",
                    "tipo_de_regla": "Dependencia",
                }
                if values:
                    payload["valores_existentes"] = values

                matched_context.append(payload)
                seen_headers.add(normalized_header)

    return matched_context


def _find_matching_columns(
    candidate_labels: Sequence[str], column_models: Sequence[TemplateColumnModel]
) -> list[TemplateColumnModel]:
    cleaned_targets = [
        label for label in candidate_labels if isinstance(label, str) and label.strip()
    ]
    if not cleaned_targets:
        return []

    exact_matches: list[TemplateColumnModel] = []
    seen_ids: set[int] = set()

    for model in column_models:
        if model.id is None:
            continue
        if any(_labels_match(candidate, model.name) for candidate in cleaned_targets):
            exact_matches.append(model)
            seen_ids.add(model.id)

    return [model for model in exact_matches if model.id in seen_ids]


def _build_reference_context(
    db: Session,
    current_user: User,
    message: str,
) -> dict[str, Any] | None:
    candidate_labels = _extract_quoted_labels(message)
    if not candidate_labels:
        return None

    creator_scope = current_user.id if current_user.is_admin() else current_user.created_by
    if creator_scope is None:
        return None

    dependency_rule_context = _build_dependency_rule_context(
        db, creator_scope, candidate_labels
    )

    column_models = (
        db.query(TemplateColumnModel)
        .options(
            joinedload(TemplateColumnModel.rules),
            joinedload(TemplateColumnModel.template),
        )
        .join(TemplateModel, TemplateModel.id == TemplateColumnModel.template_id)
        .filter(TemplateColumnModel.deleted == false())
        .filter(TemplateModel.deleted == false())
        .filter(TemplateModel.created_by == creator_scope)
        .all()
    )

    matched_columns = _find_matching_columns(candidate_labels, column_models)
    if not matched_columns and not dependency_rule_context:
        return None

    columns_context: list[dict[str, Any]] = []
    columns_context.extend(dependency_rule_context)

    matched_names = {
        _normalize_label(entry["nombre"])
        for entry in dependency_rule_context
        if isinstance(entry.get("nombre"), str)
    }
    matched_names.update(_normalize_label(model.name) for model in matched_columns)
    missing_fields = [
        label for label in candidate_labels if _normalize_label(label) not in matched_names
    ]

    for model in matched_columns:
        list_values: list[str] = []
        seen_values: set[str] = set()

        for rule_model in model.rules:
            for definition in _iter_rule_definitions(rule_model.rule):
                for item in _extract_list_values_for_header(definition, model.name):
                    normalized_item = _normalize_label(item)
                    if normalized_item in seen_values:
                        continue
                    seen_values.add(normalized_item)
                    list_values.append(item)

        column_payload: dict[str, Any] = {
            "nombre": model.name,
            "tipo_de_dato": model.data_type,
        }
        if model.description:
            column_payload["descripcion"] = model.description
        if getattr(model, "template", None) is not None:
            column_payload["template"] = model.template.name
        if list_values:
            column_payload["valores_existentes"] = list_values

        columns_context.append(column_payload)

    dependent_field, source_field = _extract_dependency_field_candidates(message)
    context: dict[str, Any] = {"columnas_relacionadas": columns_context}
    if missing_fields:
        context["campos_no_encontrados"] = missing_fields
    if dependent_field and source_field:
        context["dependencia_detectada"] = {
            "campo_dependiente": dependent_field,
            "campo_condicionante": source_field,
        }
    return context


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


def _sanitize_dependency_header(raw_response: Any) -> Any:
    """Normalize dependency headers based on the actual rule structure."""

    if not isinstance(raw_response, Mapping):
        return raw_response

    tipo_de_dato = raw_response.get("Tipo de dato") or raw_response.get("tipo_de_dato")
    if not isinstance(tipo_de_dato, str) or _normalize_label(tipo_de_dato) != "dependencia":
        return raw_response

    rule_block = raw_response.get("Regla") or raw_response.get("regla")
    if not isinstance(rule_block, Mapping):
        return raw_response

    payload = dict(raw_response)
    payload["Regla"] = rule_block

    inferred_header_rule = _infer_header_rule(payload)
    inferred_headers = _generate_dependency_headers(payload)

    if not inferred_headers and not inferred_header_rule:
        return raw_response

    sanitized = dict(raw_response)
    if inferred_header_rule:
        sanitized["Header rule"] = inferred_header_rule
    if inferred_headers:
        sanitized["Header"] = inferred_headers
    return sanitized


def _extract_existing_rule_names(rules_catalog: Sequence[Mapping[str, Any]]) -> set[str]:
    existing_names: set[str] = set()

    for group in rules_catalog:
        if not isinstance(group, Mapping):
            continue
        entries = group.get("Reglas")
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("Nombre de la regla")
            if isinstance(name, str) and name.strip():
                existing_names.add(name.strip())

    return existing_names


def _build_rule_field_candidates(payload: Mapping[str, Any]) -> list[str]:
    type_aliases = {
        _normalize_label(alias) for alias in _DEPENDENCY_TYPE_ALIASES.values()
    }
    parameter_aliases = {
        _normalize_label(label)
        for labels in (
            ("Longitud mínima", "Longitud máxima"),
            ("Valor mínimo", "Valor máximo", "Número de decimales"),
            ("Formato", "Fecha mínima", "Fecha máxima"),
            ("Código de país",),
            ("Lista", "Lista compleja"),
            ("Campos", "Columnas", "Nombre de campos"),
        )
        for label in labels
    }

    candidates = _deduplicate_headers(
        _extract_header_entries(payload.get("Header rule"))
        or _extract_header_entries(payload.get("Header"))
    )

    filtered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_label(candidate)
        if normalized in type_aliases or normalized in parameter_aliases:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        filtered.append(candidate.strip())

    if filtered:
        return filtered[:3]

    fallback_candidates: list[str] = []
    quoted_sources = [
        payload.get("Descripción"),
        payload.get("Mensaje de error"),
        payload.get("Nombre de la regla"),
    ]
    for source in quoted_sources:
        if not isinstance(source, str):
            continue
        fallback_candidates.extend(_extract_quoted_labels(source))

    ejemplo = payload.get("Ejemplo")
    if isinstance(ejemplo, Mapping):
        for key in ejemplo.keys():
            if isinstance(key, str) and key.strip():
                fallback_candidates.append(key.strip())
        for value in ejemplo.values():
            if isinstance(value, Mapping):
                for key in value.keys():
                    if isinstance(key, str) and key.strip():
                        fallback_candidates.append(key.strip())
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for item in value:
                    if isinstance(item, Mapping):
                        for key in item.keys():
                            if isinstance(key, str) and key.strip():
                                fallback_candidates.append(key.strip())

    for candidate in fallback_candidates:
        normalized = _normalize_label(candidate)
        if normalized in type_aliases or normalized in parameter_aliases:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        filtered.append(candidate.strip())

    if filtered:
        return filtered[:3]

    tipo = payload.get("Tipo de dato")
    if isinstance(tipo, str) and tipo.strip():
        return [tipo.strip()]
    return ["campo"]


def _format_rule_fields_label(fields: Sequence[str]) -> str:
    cleaned = [field.strip() for field in fields if isinstance(field, str) and field.strip()]
    if not cleaned:
        return "campo"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} y {cleaned[1]}"
    return f"{cleaned[0]}, {cleaned[1]} y {cleaned[2]}"


def _infer_rule_focus(payload: Mapping[str, Any]) -> str:
    tipo = payload.get("Tipo de dato")
    regla = payload.get("Regla")
    normalized_type = _normalize_label(tipo) if isinstance(tipo, str) else ""

    if normalized_type == "texto":
        return "longitud"
    if normalized_type == "numero":
        return "rango"
    if normalized_type == "documento":
        return "longitud"
    if normalized_type == "lista":
        return "valores permitidos"
    if normalized_type == "lista compleja":
        return "combinaciones permitidas"
    if normalized_type == "telefono":
        return "formato"
    if normalized_type == "correo":
        return "formato"
    if normalized_type == "fecha":
        return "formato"
    if normalized_type == "duplicados":
        return "unicidad"
    if normalized_type == "validacion conjunta":
        return "consistencia"
    if normalized_type != "dependencia" or not isinstance(regla, Mapping):
        return "condicion"

    specifics = regla.get("reglas especifica")
    if not isinstance(specifics, Sequence) or isinstance(specifics, (str, bytes)):
        return "condicion"

    has_list = False
    focus_by_type: str | None = None
    for entry in specifics:
        if not isinstance(entry, Mapping):
            continue
        for key, value in entry.items():
            if not isinstance(key, str):
                continue
            normalized_key = _normalize_label(key)
            if normalized_key == "lista":
                has_list = True
            if normalized_key in {"texto", "documento"} and isinstance(value, Mapping):
                focus_by_type = "longitud condicional"
            elif normalized_key == "numero" and isinstance(value, Mapping):
                focus_by_type = "rango condicional"
            elif normalized_key in {"telefono", "correo", "fecha"} and isinstance(value, Mapping):
                focus_by_type = "formato condicional"

            if isinstance(value, Mapping):
                for nested_key, nested_value in value.items():
                    if not isinstance(nested_key, str):
                        continue
                    if isinstance(nested_value, list):
                        has_list = True

    if focus_by_type:
        return focus_by_type
    if has_list:
        return "valores permitidos"
    return "condicion"


def _compress_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _compose_rule_name(type_label: str, fields_label: str, focus: str, template: str) -> str:
    return _compress_spaces(
        template.format(
            type=type_label.strip(),
            fields=fields_label.strip(),
            focus=focus.strip(),
        )
    )


def _build_rule_name_variants(type_label: str, fields_label: str, focus: str) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = _normalize_label(candidate)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        variants.append(candidate)

    for template in _RULE_NAME_PARAPHRASE_TEMPLATES:
        add(_compose_rule_name(type_label, fields_label, focus, template))

    for synonym in _RULE_NAME_FOCUS_SYNONYMS.get(_normalize_label(focus), ()):
        for template in _RULE_NAME_PARAPHRASE_TEMPLATES:
            add(_compose_rule_name(type_label, fields_label, synonym, template))

    add(_compress_spaces(f"Validación {type_label} {fields_label} específica"))
    add(_compress_spaces(f"Validación {type_label} {fields_label} aplicada"))
    return variants


def _normalize_rule_name(
    raw_response: Any,
    existing_rule_names: Sequence[str] | None = None,
) -> Any:
    if not isinstance(raw_response, Mapping):
        return raw_response

    tipo = raw_response.get("Tipo de dato")
    if not isinstance(tipo, str) or not tipo.strip():
        return raw_response

    fields_label = _format_rule_fields_label(_build_rule_field_candidates(raw_response))
    focus = _infer_rule_focus(raw_response)
    variants = _build_rule_name_variants(tipo.strip(), fields_label, focus)
    if not variants:
        return raw_response

    existing_normalized = {
        _normalize_label(name)
        for name in (existing_rule_names or [])
        if isinstance(name, str) and name.strip()
    }
    original_name = raw_response.get("Nombre de la regla")
    if isinstance(original_name, str) and original_name.strip():
        existing_normalized.discard(_normalize_label(original_name))

    selected_name = variants[0]
    for candidate in variants:
        if _normalize_label(candidate) not in existing_normalized:
            selected_name = candidate
            break

    sanitized = dict(raw_response)
    sanitized["Nombre de la regla"] = selected_name
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


# Clean overrides for message parsing and validation.
def _extract_quoted_labels(message: str) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()

    patterns = (
        r"[\"'“”‘’]([^\"'“”‘’]{2,80})[\"'“”‘’]",
        r"\bcampo\s+([A-Za-z0-9_ ]{2,80}?)(?=;|,| si | debe | dependa | dependa del |$)",
    )

    for pattern in patterns:
        for match in re.finditer(pattern, message, flags=re.IGNORECASE):
            candidate = match.group(1).strip(" .;,:")
            normalized = _normalize_label(candidate)
            if not candidate or normalized in seen:
                continue
            seen.add(normalized)
            labels.append(candidate)

    return labels


def _infer_supported_data_types(message: str) -> set[str]:
    normalized = _normalize_label(message)
    inferred_types: set[str] = set()

    for type_label, hints in _SUPPORTED_TYPE_HINTS.items():
        for hint in hints:
            if _normalize_label(hint) in normalized:
                inferred_types.add(type_label)
                break

    if re.search(r"\b\d+(?:[.,]\d+)?\b", normalized):
        inferred_types.add("Numero")

    return inferred_types


def _validate_message_semantics(message: str) -> str | None:
    normalized = _normalize_label(message)
    inferred_types = _infer_supported_data_types(message)
    actionable_detail = _has_actionable_rule_detail(message)
    vague_detail = any(marker in normalized for marker in _VAGUE_RULE_MARKERS)

    if vague_detail and not actionable_detail:
        return (
            "El mensaje tiene forma de regla, pero no define una validacion concreta. "
            "Indica una condicion real, por ejemplo limites, formato, valores permitidos o una dependencia clara."
        )

    if not actionable_detail:
        return (
            "El mensaje no describe una validacion accionable. "
            "Falta indicar una restriccion concreta como rango, formato, lista permitida, unicidad o dependencia."
        )

    if not inferred_types:
        return (
            "No se pudo relacionar el mensaje con un tipo de dato soportado. "
            "Especifica mejor si se trata de texto, numero, documento, lista, fecha, correo, telefono, dependencia, validacion conjunta o duplicados."
        )

    return None


def _validate_dependency_message_coherence(message: str) -> str | None:
    normalized = _normalize_label(message)
    dependency_markers = ("depend", "dependa", "depender", "dependencia")
    if not any(marker in normalized for marker in dependency_markers):
        return None

    dependent_field, source_field = _extract_dependency_field_candidates(message)
    if not dependent_field or not source_field:
        return (
            "El mensaje parece pedir una dependencia, pero no identifica con claridad "
            "dos campos distintos para relacionarlos."
        )

    if _labels_match(dependent_field, source_field):
        return (
            "La dependencia debe involucrar dos campos distintos. "
            "El mensaje actual repite el mismo campo."
        )

    logical_markers = (
        " si ",
        " debe ",
        " debera ",
        " cuando ",
        " en caso contrario ",
        " mostrar ",
        " mensaje ",
        " unicamente ",
        " distinto ",
        " diferente ",
    )
    if not any(marker in f" {normalized} " for marker in logical_markers):
        return (
            "El mensaje identifica campos, pero no describe una condicion o consecuencia "
            "lo bastante clara para construir la dependencia."
        )

    return None


@router.post("/analyze", response_model=AssistantMessageResponse)
def analyze_message(
    payload: AssistantMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    assistant: StructuredChatService = Depends(get_structured_chat_service),
) -> AssistantMessageResponse:
    """Genera una respuesta estructurada que indica cómo atender el mensaje del usuario."""

    try:
        semantics_error = _validate_message_semantics(payload.message)
        if semantics_error:
            raise OffTopicMessageError(semantics_error)

        coherence_error = _validate_dependency_message_coherence(payload.message)
        if coherence_error:
            raise OffTopicMessageError(coherence_error)

        reference_context = _build_reference_context(db, current_user, payload.message)
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
            reference_context=reference_context,
        )
        logger.debug("Respuesta sin validar del asistente: %s", raw_response)
        raw_response = _sanitize_dependency_header(raw_response)
        raw_response = _normalize_rule_name(
            raw_response,
            existing_rule_names=_extract_existing_rule_names(serialized_rules),
        )
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
