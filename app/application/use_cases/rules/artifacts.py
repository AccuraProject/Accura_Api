"""Helpers to build rule summaries and downloadable attachments."""

from __future__ import annotations

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


def _build_summary_for_definition(definition: Mapping[str, Any]) -> str:
    name = _safe_text(definition.get("Nombre de la regla")) or "Regla sin nombre"
    rule_type = _safe_text(definition.get("Tipo de dato")) or "No especificado"
    required = "si" if bool(definition.get("Campo obligatorio")) else "no"
    error_message = _safe_text(definition.get("Mensaje de error"))
    description = _safe_text(definition.get("Descripcion")) or _safe_text(
        definition.get("Descripci\u00f3n")
    )
    rule_block = definition.get("Regla")
    normalized_type = _normalize_label(rule_type)

    details = ""
    if isinstance(rule_block, Mapping):
        if normalized_type == "lista":
            values = rule_block.get("Lista")
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                details = f"Solo acepta valores de una lista cerrada con {len(values)} opciones."
        elif normalized_type in {"lista compleja", "lista completa"}:
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
                if headers:
                    details = (
                        "Valida combinaciones permitidas entre "
                        + ", ".join(_safe_text(header) for header in headers)
                        + f" en {len(values)} registros cargados."
                    )
                else:
                    details = f"Valida combinaciones permitidas en {len(values)} registros cargados."
        elif normalized_type == "dependencia":
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
                if control_fields:
                    details = (
                        "Aplica reglas segun el valor de "
                        + ", ".join(control_fields)
                        + f" con {len(specifics)} escenarios configurados."
                    )
                else:
                    details = f"Aplica reglas condicionales con {len(specifics)} escenarios configurados."
        else:
            detail_parts = [
                f"{key}: {_safe_text(value)}"
                for key, value in rule_block.items()
                if isinstance(key, str)
                and not isinstance(value, (Mapping, list, tuple, set))
            ]
            if detail_parts:
                details = "Parametros principales: " + "; ".join(detail_parts) + "."

    pieces = [
        f"La regla '{name}' valida datos de tipo {rule_type}.",
        f"Campo obligatorio: {required}.",
    ]
    if description:
        pieces.append(description.rstrip(".") + ".")
    if details:
        pieces.append(details)
    if error_message:
        pieces.append(f"Si falla, muestra: {error_message}.")
    return " ".join(piece.strip() for piece in pieces if piece.strip())


def build_rule_artifacts(
    rule_payload: dict[str, Any] | list[Any],
    *,
    rule_id: int | None,
) -> tuple[str | None, str | None]:
    """Build the summary text and downloadable attachment for a rule payload."""

    definitions = _iter_rule_definitions(rule_payload)
    summaries = [_build_summary_for_definition(definition) for definition in definitions]

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

    summary = "\n".join(item for item in summaries if item).strip() or None
    return summary, attachment_url


__all__ = ["build_rule_artifacts"]
