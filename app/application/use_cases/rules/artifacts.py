"""Helpers to build rule summaries and downloadable attachments."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any
from uuid import uuid4

from openpyxl import Workbook

from app.infrastructure.storage import build_downloadable_blob_url, upload_blob

_EXCEL_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_RULE_ATTACHMENT_PREFIX = "Rules"
_LIST_TYPE_LABELS = {"lista", "lista compleja", "dependencia"}
_DEPENDENCY_TYPE_ALIASES = {
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


def _normalize_label(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_label = "".join(char for char in normalized if not unicodedata.combining(char))
    collapsed = re.sub(r"[\s\-_/]+", " ", ascii_label)
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


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value)


def _extract_dependency_specifics(rule_block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key, value in rule_block.items():
        if isinstance(key, str) and _normalize_label(key) == "reglas especifica":
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return [entry for entry in value if isinstance(entry, Mapping)]
        if isinstance(value, Mapping):
            nested = _extract_dependency_specifics(value)
            if nested:
                return nested
    return []


def _extract_attachment_payload(
    definition: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]]] | None:
    rule_type = _normalize_label(definition.get("Tipo de dato", ""))
    rule_block = definition.get("Regla")
    if not isinstance(rule_block, Mapping) or rule_type not in _LIST_TYPE_LABELS:
        return None

    if rule_type == "lista":
        values = rule_block.get("Lista")
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return None
        rows = [{"Lista": _safe_text(value)} for value in values if _safe_text(value)]
        return (["Lista"], rows) if rows else None

    if rule_type == "lista compleja":
        values = None
        for key, candidate in rule_block.items():
            if isinstance(key, str) and _normalize_label(key) in {"lista compleja", "lista completa"}:
                values = candidate
                break
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return None
        items = [entry for entry in values if isinstance(entry, Mapping)]
        if not items:
            return None
        headers: list[str] = []
        seen: set[str] = set()
        for item in items:
            for key in item.keys():
                if not isinstance(key, str):
                    continue
                label = key.strip()
                if not label:
                    continue
                normalized = _normalize_label(label)
                if normalized in seen:
                    continue
                seen.add(normalized)
                headers.append(label)
        rows = [{header: item.get(header) for header in headers} for item in items]
        return (headers, rows) if rows else None

    specifics = _extract_dependency_specifics(rule_block)
    if not specifics:
        return None

    header_set: set[str] = set()
    ordered_headers: list[str] = []
    rows: list[dict[str, Any]] = []
    for entry in specifics:
        row: dict[str, Any] = {}
        for key, value in entry.items():
            if not isinstance(key, str):
                continue
            label = key.strip()
            if not label:
                continue
            normalized = _normalize_label(label)
            if normalized in _DEPENDENCY_TYPE_ALIASES and isinstance(value, Mapping):
                nested_list = value.get("Lista")
                if isinstance(nested_list, Sequence) and not isinstance(
                    nested_list, (str, bytes)
                ):
                    row[label] = ", ".join(_safe_text(item) for item in nested_list if _safe_text(item))
                else:
                    nested_complex = None
                    for nested_key, nested_value in value.items():
                        if isinstance(nested_key, str) and _normalize_label(nested_key) in {
                            "lista compleja",
                            "lista completa",
                        }:
                            nested_complex = nested_value
                            break
                    if isinstance(nested_complex, Sequence) and not isinstance(
                        nested_complex, (str, bytes)
                    ):
                        row[label] = " | ".join(
                            ", ".join(
                                f"{nested_label}: {_safe_text(nested_item.get(nested_label))}"
                                for nested_label in nested_item.keys()
                                if isinstance(nested_label, str)
                            )
                            for nested_item in nested_complex
                            if isinstance(nested_item, Mapping)
                        )
                    else:
                        row[label] = ", ".join(
                            f"{nested_label}: {_safe_text(nested_value)}"
                            for nested_label, nested_value in value.items()
                            if isinstance(nested_label, str)
                        )
            else:
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    row[label] = ", ".join(_safe_text(item) for item in value if _safe_text(item))
                else:
                    row[label] = _safe_text(value)

            if label not in header_set:
                header_set.add(label)
                ordered_headers.append(label)
        if row:
            rows.append(row)

    return (ordered_headers, rows) if rows else None


def _build_workbook(headers: list[str], rows: list[dict[str, Any]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Datos"
    worksheet.append(headers)
    for row in rows:
        worksheet.append([row.get(header) for header in headers])

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _serialize_summary_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            _safe_text(key): _serialize_summary_value(nested)
            for key, nested in value.items()
            if _safe_text(key)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_serialize_summary_value(item) for item in value]
    return value


def _normalize_example_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, nested in value.items():
            normalized_key = key
            if isinstance(key, str):
                normalized_label = _normalize_label(key)
                if normalized_label in {"valido", "ejemplo valido"}:
                    normalized_key = "Ejemplo válido"
                elif normalized_label in {"invalido", "ejemplo invalido"}:
                    normalized_key = "Ejemplo inválido"
            normalized[_safe_text(normalized_key) or key] = _normalize_example_payload(nested)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_normalize_example_payload(item) for item in value]
    return value


def normalize_rule_examples_payload(rule_payload: Any) -> Any:
    if isinstance(rule_payload, Mapping):
        normalized = dict(rule_payload)
        if "Ejemplo" in normalized:
            normalized["Ejemplo"] = _normalize_example_payload(normalized.get("Ejemplo"))
        if "Regla" in normalized:
            normalized["Regla"] = normalize_rule_examples_payload(normalized.get("Regla"))
        return normalized
    if isinstance(rule_payload, Sequence) and not isinstance(rule_payload, (str, bytes)):
        return [normalize_rule_examples_payload(entry) for entry in rule_payload]
    return rule_payload


def _humanize_value(value: Any) -> str:
    if value is None:
        return "No definido"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if isinstance(value, Mapping):
        if not value:
            return "Sin configuración adicional"
        return "Configuración estructurada"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            return "Sin elementos"
        preview = ", ".join(_safe_text(item) for item in value[:5])
        if len(value) > 5:
            preview += ", ..."
        return preview
    return _safe_text(value) or "No definido"


def _join_readable_values(values: Sequence[Any], *, limit: int = 5) -> str:
    readable = [_safe_text(value) for value in values if _safe_text(value)]
    if not readable:
        return "No definido"
    preview = ", ".join(readable[:limit])
    if len(readable) > limit:
        preview += ", ..."
    return preview


def _append_main_parameter(
    container: list[dict[str, str]],
    *,
    key: str,
    value: Any,
) -> None:
    normalized_key = _safe_text(key)
    if not normalized_key:
        return
    container.append(
        {
            "key": normalized_key,
            "value": _humanize_value(value),
        }
    )


def _build_homologated_main_parameters(
    *,
    rule_type: str,
    rule_block: Any,
    interpreted_config: Mapping[str, Any],
) -> list[dict[str, str]]:
    normalized_type = _normalize_label(rule_type)
    parameters: list[dict[str, str]] = []

    if normalized_type == "lista":
        allowed_values = interpreted_config.get("allowed_values")
        if isinstance(allowed_values, Sequence) and not isinstance(allowed_values, (str, bytes)):
            _append_main_parameter(
                parameters,
                key="Valores permitidos",
                value=_join_readable_values(allowed_values),
            )
            _append_main_parameter(
                parameters,
                key="Cantidad de valores",
                value=len(allowed_values),
            )
        return parameters

    if normalized_type in {"lista compleja", "lista completa"}:
        combination_headers = interpreted_config.get("combination_headers")
        combinations_count = interpreted_config.get("combinations_count")
        if isinstance(combination_headers, Sequence) and not isinstance(
            combination_headers, (str, bytes)
        ):
            _append_main_parameter(
                parameters,
                key="Campos de combinación",
                value=_join_readable_values(combination_headers),
            )
        if combinations_count is not None:
            _append_main_parameter(
                parameters,
                key="Cantidad de combinaciones",
                value=combinations_count,
            )
        return parameters

    if normalized_type == "dependencia":
        control_fields = interpreted_config.get("control_fields")
        scenarios_count = interpreted_config.get("scenarios_count")
        if isinstance(control_fields, Sequence) and not isinstance(control_fields, (str, bytes)):
            _append_main_parameter(
                parameters,
                key="Campos de control",
                value=_join_readable_values(control_fields),
            )
        if scenarios_count is not None:
            _append_main_parameter(
                parameters,
                key="Escenarios configurados",
                value=scenarios_count,
            )
        return parameters

    if isinstance(rule_block, Mapping):
        for key, value in rule_block.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, (Mapping, list, tuple, set)):
                continue
            _append_main_parameter(parameters, key=key, value=value)

    return parameters


def _collect_configuration_items(
    value: Any,
    *,
    path: str = "",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    if isinstance(value, Mapping):
        for key, nested in value.items():
            label = _safe_text(key)
            if not label:
                continue
            current_path = f"{path}.{label}" if path else label
            items.append(
                {
                    "field": label,
                    "path": current_path,
                    "current_value": _serialize_summary_value(nested),
                    "explained_value": _humanize_value(nested),
                }
            )
            items.extend(_collect_configuration_items(nested, path=current_path))
        return items

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            current_path = f"{path}[{index}]"
            items.append(
                {
                    "field": f"item_{index + 1}",
                    "path": current_path,
                    "current_value": _serialize_summary_value(item),
                    "explained_value": _humanize_value(item),
                }
            )
            items.extend(_collect_configuration_items(item, path=current_path))

    return items


def _infer_expected_value(rule_type: str) -> str:
    normalized = _normalize_label(rule_type)
    mapping = {
        "texto": "Un texto que cumpla las reglas de longitud o formato configuradas.",
        "numero": "Un valor numérico dentro de los límites permitidos.",
        "documento": "Un identificador o documento con la longitud y estructura esperadas.",
        "lista": "Uno de los valores definidos en la lista permitida.",
        "lista compleja": "Una combinación exacta de valores entre varios campos permitidos por la regla.",
        "lista completa": "Una combinación exacta de valores entre varios campos permitidos por la regla.",
        "telefono": "Un número telefónico válido según país y longitud configurada.",
        "correo": "Una dirección de correo válida según el formato esperado.",
        "fecha": "Una fecha que cumpla el formato configurado.",
        "dependencia": "Un valor válido dependiendo de lo que tenga otro campo relacionado.",
        "validacion conjunta": "Un conjunto de campos coherentes entre sí.",
        "duplicados": "Un registro cuya combinación de campos no se repita donde la regla lo prohíbe.",
    }
    return mapping.get(normalized, "Un valor que cumpla las condiciones definidas por la regla.")


def _infer_failure_reason(rule_type: str) -> str:
    normalized = _normalize_label(rule_type)
    mapping = {
        "texto": "Falla si el texto viene vacío cuando es obligatorio o no cumple longitud o formato.",
        "numero": "Falla si el valor no es numérico o está fuera del rango permitido.",
        "documento": "Falla si el documento no coincide con la longitud o estructura esperadas.",
        "lista": "Falla si el valor no está dentro de la lista de opciones permitidas.",
        "lista compleja": "Falla si la combinación de valores no existe dentro de las combinaciones autorizadas.",
        "lista completa": "Falla si la combinación de valores no existe dentro de las combinaciones autorizadas.",
        "telefono": "Falla si no cumple longitud, prefijo o formato telefónico esperado.",
        "correo": "Falla si no parece un correo válido o supera restricciones configuradas.",
        "fecha": "Falla si la fecha no coincide con el formato esperado.",
        "dependencia": "Falla si el valor no cumple la condición que depende de otro campo.",
        "validacion conjunta": "Falla si el conjunto de campos no cumple la coherencia esperada.",
        "duplicados": "Falla si se repite una combinación de campos marcada como única.",
    }
    return mapping.get(normalized, "Falla cuando el dato no cumple la configuración definida.")


def _build_user_examples(
    *,
    rule_type: str,
    required: bool,
    interpreted_config: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    normalized = _normalize_label(rule_type)
    valid_examples: list[str] = ["Valor de ejemplo válido"]
    invalid_examples: list[str] = ["Valor de ejemplo no válido"]

    if normalized == "lista":
        allowed_values = interpreted_config.get("allowed_values")
        if isinstance(allowed_values, Sequence) and allowed_values and not isinstance(allowed_values, (str, bytes)):
            valid_examples = [_safe_text(value) for value in allowed_values[:3] if _safe_text(value)] or valid_examples
            invalid_examples = ["Un valor por fuera de la lista permitida"]
    elif normalized in {"lista compleja", "lista completa"}:
        combinations = interpreted_config.get("allowed_combinations")
        if isinstance(combinations, Sequence) and combinations and not isinstance(combinations, (str, bytes)):
            valid_examples = [_humanize_value(item) for item in combinations[:2]]
            invalid_examples = ["Una combinación de campos que no aparece en la lista autorizada"]
    elif normalized == "dependencia":
        scenarios = interpreted_config.get("scenarios")
        if isinstance(scenarios, Sequence) and scenarios and not isinstance(scenarios, (str, bytes)):
            valid_examples = [_humanize_value(item) for item in scenarios[:2]]
            invalid_examples = ["Un valor que no cumple la condición definida para ese caso"]
    elif normalized == "fecha":
        valid_examples = ["2026-04-13", "2025-12-01"]
        invalid_examples = ["13/40/2026", "abril 13"]
    elif normalized == "correo":
        valid_examples = ["usuario@empresa.com", "nombre.apellido@dominio.co"]
        invalid_examples = ["usuario@", "correo-sin-arroba.com"]
    elif normalized == "telefono":
        valid_examples = ["+573001234567", "+571234567890"]
        invalid_examples = ["123", "telefono"]
    elif normalized == "numero":
        valid_examples = ["150", "25.5"]
        invalid_examples = ["texto", "12,34,56"]
    elif normalized == "texto":
        valid_examples = ["Texto válido", "Nombre completo"]
        invalid_examples = ["", "   "] if required else ["Texto con formato no permitido"]
    elif normalized == "documento":
        valid_examples = ["12345678", "CC10203040"]
        invalid_examples = ["ABC", ""]
    elif normalized == "duplicados":
        valid_examples = ["Un registro cuya combinación de campos es única"]
        invalid_examples = ["Un registro que repite una combinación ya existente"]
    elif normalized == "validacion conjunta":
        valid_examples = ["Una combinación coherente entre campos relacionados"]
        invalid_examples = ["Una combinación inconsistente entre campos relacionados"]

    if required and "" not in invalid_examples:
        invalid_examples = [*invalid_examples, "Valor vacío"]

    return valid_examples, invalid_examples


def _build_short_summary_text(
    *,
    name: str,
    rule_type: str,
    required: bool,
    description: str,
    business_description: str,
) -> str:
    parts = [
        f"La regla '{name}' valida información de tipo {rule_type or 'No especificado'}.",
        business_description,
        "El campo es obligatorio." if required else "El campo no es obligatorio en todos los casos.",
    ]
    if description:
        parts.append(description.rstrip(".") + ".")
    return " ".join(part.strip() for part in parts if part.strip())


def _build_summary_for_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    name = _safe_text(definition.get("Nombre de la regla")) or "Regla sin nombre"
    rule_type = _safe_text(definition.get("Tipo de dato")) or "No especificado"
    required = bool(definition.get("Campo obligatorio"))
    error_message = _safe_text(definition.get("Mensaje de error"))
    description = _safe_text(definition.get("Descripcion")) or _safe_text(
        definition.get("Descripci\u00f3n")
    )
    rule_block = definition.get("Regla")
    normalized_type = _normalize_label(rule_type)
    headers = definition.get("Header")
    header_rule = definition.get("Header rule")

    explained_rule_type = "general"
    business_description = "Valida una regla con la configuracion registrada."
    interpreted_config: dict[str, Any] = {}
    readable_details: list[str] = []

    if isinstance(rule_block, Mapping):
        if normalized_type == "lista":
            explained_rule_type = "lista_cerrada"
            values = rule_block.get("Lista")
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                interpreted_config = {
                    "allowed_values_count": len(values),
                    "allowed_values": [_safe_text(value) for value in values if _safe_text(value)],
                }
                business_description = (
                    "Solo acepta valores predefinidos dentro de una lista cerrada."
                )
                readable_details.append(
                    f"Valores permitidos configurados: {len(interpreted_config['allowed_values'])}."
                )
        elif normalized_type in {"lista compleja", "lista completa"}:
            explained_rule_type = "lista_compleja"
            values = next(
                (
                    candidate
                    for key, candidate in rule_block.items()
                    if isinstance(key, str)
                    and _normalize_label(key) in {"lista compleja", "lista completa"}
                ),
                None,
            )
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                first_item = next((item for item in values if isinstance(item, Mapping)), None)
                headers = list(first_item.keys()) if isinstance(first_item, Mapping) else []
                interpreted_config = {
                    "combinations_count": len(values),
                    "combination_headers": [_safe_text(header) for header in headers if _safe_text(header)],
                    "allowed_combinations": _serialize_summary_value(values),
                }
                business_description = (
                    "Valida combinaciones especificas permitidas entre varios campos."
                )
                readable_details.append(
                    f"Combinaciones permitidas configuradas: {interpreted_config['combinations_count']}."
                )
        elif normalized_type == "dependencia":
            explained_rule_type = "dependencia"
            specifics = _extract_dependency_specifics(rule_block)
            if specifics:
                control_fields: list[str] = []
                for entry in specifics:
                    for key in entry.keys():
                        if not isinstance(key, str):
                            continue
                        normalized_key = _normalize_label(key)
                        if normalized_key in _DEPENDENCY_TYPE_ALIASES:
                            continue
                        label = key.strip()
                        if label and label not in control_fields:
                            control_fields.append(label)
                interpreted_config = {
                    "scenarios_count": len(specifics),
                    "control_fields": control_fields,
                    "scenarios": _serialize_summary_value(specifics),
                }
                business_description = (
                    "Aplica validaciones condicionales segun el valor de uno o mas campos."
                )
                readable_details.append(
                    f"Escenarios condicionales configurados: {interpreted_config['scenarios_count']}."
                )
        else:
            detail_parts = [
                {
                    "key": key,
                    "value": _safe_text(value),
                }
                for key, value in rule_block.items()
                if isinstance(key, str)
                and not isinstance(value, (Mapping, list, tuple, set))
            ]
            if detail_parts:
                business_description = (
                    "Valida el dato con base en parametros de configuracion directos."
                )
                readable_details.append(
                    f"Parámetros directos configurados: {len(detail_parts)}."
                )

    interpreted_config = {
        **interpreted_config,
        "main_parameters": _build_homologated_main_parameters(
            rule_type=rule_type,
            rule_block=rule_block,
            interpreted_config=interpreted_config,
        ),
    }

    user_examples = _build_user_examples(
        rule_type=rule_type,
        required=required,
        interpreted_config=interpreted_config,
    )
    visible_fields = _serialize_summary_value(headers) if headers is not None else []
    support_fields = _serialize_summary_value(header_rule) if header_rule is not None else []
    valid_examples, invalid_examples = user_examples

    return {
        "rule_name": name,
        "technical_type": rule_type,
        "summary": _build_short_summary_text(
            name=name,
            rule_type=rule_type,
            required=required,
            description=description,
            business_description=business_description,
        ),
        "description": description or business_description,
        "type": rule_type,
        "required": required,
        "what_it_validates": _infer_expected_value(rule_type),
        "when_it_fails": _infer_failure_reason(rule_type),
        "error_message": error_message or None,
        "valid_examples": valid_examples,
        "invalid_examples": invalid_examples,
        "main_fields": visible_fields,
        "support_fields": support_fields,
        "configuration_summary": readable_details,
        "configuration": {
            "interpreted": interpreted_config,
            "original": _serialize_summary_value(rule_block) if rule_block is not None else {},
        },
    }


def build_rule_summary_payload(
    rule_payload: dict[str, Any] | list[Any],
) -> dict[str, Any] | None:
    definitions = _iter_rule_definitions(rule_payload)
    rules = [_build_summary_for_definition(definition) for definition in definitions]
    if not rules:
        return None
    return {
        "version": 1,
        "rule_count": len(rules),
        "rules": rules,
    }


def build_rule_artifacts(
    rule_payload: dict[str, Any] | list[Any],
    *,
    rule_id: int | None,
) -> tuple[str | None, str | None]:
    """Build the summary text and downloadable attachment for a rule payload."""

    definitions = _iter_rule_definitions(rule_payload)
    summary_payload = build_rule_summary_payload(rule_payload)

    attachment_headers: list[str] | None = None
    attachment_rows: list[dict[str, Any]] = []
    for definition in definitions:
        extracted = _extract_attachment_payload(definition)
        if not extracted:
            continue
        headers, rows = extracted
        if attachment_headers is None:
            attachment_headers = list(headers)
        for header in headers:
            if header not in attachment_headers:
                attachment_headers.append(header)
        attachment_rows.extend(rows)

    attachment_url: str | None = None
    if attachment_headers and attachment_rows:
        data = _build_workbook(attachment_headers, attachment_rows)
        reference = rule_id if rule_id is not None else uuid4().hex
        blob_path = f"{_RULE_ATTACHMENT_PREFIX}/{reference}/rule_attachment.xlsx"
        upload_blob(blob_path, data, content_type=_EXCEL_CONTENT_TYPE)
        attachment_url = build_downloadable_blob_url(
            blob_path,
            download_name=f"rule_{reference}_attachment.xlsx",
        )

    summary = json.dumps(summary_payload, ensure_ascii=False) if summary_payload else None
    return summary, attachment_url


__all__ = ["build_rule_artifacts", "build_rule_summary_payload"]
