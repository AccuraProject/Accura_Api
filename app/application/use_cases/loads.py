"""Use cases orchestrating load executions for templates."""

from __future__ import annotations

import importlib
import json
import logging
import math
import re
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path
from difflib import SequenceMatcher
from functools import lru_cache
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, MetaData, Numeric, Table, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.use_cases.notifications import (
    notify_load_status_changed,
    notify_load_validated_success,
    notify_template_processing,
)
from app.application.use_cases.template_columns.naming import (
    derive_column_identifier,
)
from app.domain.entities import (
    LOAD_STATUS_FAILED,
    LOAD_STATUS_PROCESSING,
    LOAD_STATUS_VALIDATED_SUCCESS,
    LOAD_STATUS_VALIDATED_WITH_ERRORS,
    Load,
    LoadedFile,
    Template,
    TemplateColumn,
    User,
)
from app.infrastructure.database import engine
from app.infrastructure.repositories import (
    LoadRepository,
    LoadedFileRepository,
    RuleRepository,
    TemplateRepository,
    TemplateUserAccessRepository,
    UserRepository,
)
from app.infrastructure.storage import download_blob_to_path, upload_blob
from app.utils import now_in_app_timezone

_SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv"}
_REPORT_PREFIX = "Reports"
_EXCEL_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CSV_CONTENT_TYPE = "text/csv"
_DOWNLOAD_DIRECTORY = Path(tempfile.gettempdir()) / "accura_api_downloads"
_GROUPED_HEADER_RULE_TYPES = {"lista compleja", "lista completa", "dependencia"}
logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    """Return ``name`` transformed into a filesystem-safe slug."""

    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip())
    return cleaned.strip("_")


def _build_display_excel_name(name: str, *, default: str) -> str:
    base = name.strip() or default
    lower = base.lower()
    if lower.endswith(".xlsx"):
        base = base[:-5]
    elif lower.endswith(".xls"):
        base = base[:-4]
    return f"{base}.xlsx"


@lru_cache(maxsize=1)
def _get_pandas_module() -> Any:
    """Load :mod:`pandas` lazily to avoid import-time circular errors."""

    return importlib.import_module("pandas")


def _report_storage_filename(load_id: int, template_name: str) -> str:
    sanitized = _sanitize_filename(template_name)
    core = sanitized or "reporte"
    return f"load_{load_id}_{core}.xlsx"


def _report_display_filename(template_name: str) -> str:
    return _build_display_excel_name(template_name, default="Reporte")


def _original_storage_filename(load_id: int, original_filename: str, extension: str) -> str:
    stem = Path(original_filename).stem
    sanitized = _sanitize_filename(stem)
    core = sanitized or "original"
    return f"load_{load_id}_{core}{extension}"


def _original_download_name(load: Load, extension: str) -> str:
    original_name = Path(load.file_name).name
    if original_name:
        return original_name
    if load.id is None:
        raise ValueError("Identificador de carga inválido")
    return f"carga_{load.id}{extension}"


def _build_report_blob_path(
    table_name: str, template_id: int, user_id: int, filename: str
) -> str:
    return f"{_REPORT_PREFIX}/{table_name}/{template_id}-{user_id}-{filename}"


def _download_to_tempfile(blob_path: str, suffix: str) -> Path:
    _DOWNLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = _DOWNLOAD_DIRECTORY / f"{uuid4().hex}{suffix}"
    return download_blob_to_path(blob_path, destination)


if TYPE_CHECKING:
    from pandas import DataFrame  # pragma: no cover
else:  # pragma: no cover
    DataFrame = Any


def upload_template_load(
    session: Session,
    *,
    template_id: int,
    user: User,
    file_bytes: bytes,
    filename: str,
) -> Load:
    """Register a new load for ``template_id`` and mark it as pending processing."""

    template = _get_template(session, template_id)
    _ensure_user_has_access(session, template, user)

    if not filename:
        raise ValueError("Nombre de archivo no proporcionado")

    suffix = Path(filename).suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            "Formato de archivo no soportado. Usa archivos .xlsx, .xls o .csv"
        )

    columns = _ordered_active_columns(template)
    if not columns:
        raise ValueError("La plantilla no tiene columnas activas para importar")

    load_repo = LoadRepository(session)
    now = now_in_app_timezone()
    load = load_repo.create(
        Load(
            id=None,
            template_id=template.id,
            user_id=user.id,
            status=LOAD_STATUS_PROCESSING,
            file_name=filename,
            total_rows=0,
            error_rows=0,
            report_path=None,
            created_at=now,
            started_at=now,
            finished_at=None,
        )
    )

    notify_template_processing(
        session, load=load, template=template, user=user
    )
    return load


def process_template_load(
    session: Session,
    *,
    load_id: int,
    template_id: int,
    user_id: int,
    file_bytes: bytes,
    filename: str,
) -> Load:
    """Execute the validation flow for an existing load."""

    started_processing = perf_counter()
    load_repo = LoadRepository(session)
    load = load_repo.get(load_id)
    if load is None:
        raise ValueError("Carga no encontrada")

    template = _get_template(session, template_id)
    user = UserRepository(session).get(user_id)
    if user is None:
        raise ValueError("Usuario no encontrado")

    _ensure_user_has_access(session, template, user)

    suffix = Path(filename).suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            "Formato de archivo no soportado. Usa archivos .xlsx, .xls o .csv"
        )

    columns = _ordered_active_columns(template)
    if not columns:
        raise ValueError("La plantilla no tiene columnas activas para importar")

    try:
        logger.info(
            "Load %s started for file '%s' (%s bytes) on template '%s'",
            load_id,
            filename,
            len(file_bytes),
            template.table_name,
        )
        stage_started = perf_counter()
        dataframe = _read_source_file(file_bytes, suffix)
        logger.info(
            "Load %s read source file in %.3fs",
            load_id,
            perf_counter() - stage_started,
        )

        stage_started = perf_counter()
        dataframe = _normalize_dataframe(dataframe)
        _ensure_dataframe_has_rows(dataframe)
        _validate_headers(dataframe, columns)
        logger.info(
            "Load %s normalized data and validated headers in %.3fs",
            load_id,
            perf_counter() - stage_started,
        )

        stage_started = perf_counter()
        rules = _load_rules(session, columns)
        logger.info(
            "Load %s loaded %s active rules in %.3fs",
            load_id,
            len(rules),
            perf_counter() - stage_started,
        )

        stage_started = perf_counter()
        validated_df, row_is_valid = _validate_dataframe(dataframe, columns, rules)
        logger.info(
            "Load %s validated %s rows across %s columns in %.3fs",
            load_id,
            len(validated_df.index),
            len(columns),
            perf_counter() - stage_started,
        )

        operation_number = load.id or 0
        if not operation_number:
            raise ValueError("Identificador de carga inválido")
        identifier_map = {
            column.name: derive_column_identifier(column.name) for column in columns
        }

        stage_started = perf_counter()
        processed_rows = _persist_rows(
            validated_df,
            row_is_valid,
            template.table_name,
            operation_number=operation_number,
            column_identifier_map=identifier_map,
        )
        logger.info(
            "Load %s persisted %s rows to temporary table '%s' in %.3fs",
            load_id,
            len(validated_df.index),
            template.table_name,
            perf_counter() - stage_started,
        )

        stage_started = perf_counter()
        report_df = _prepare_report_dataframe(
            validated_df, operation_number=operation_number
        )
        source_df = _prepare_original_dataframe(validated_df)

        report_blob_path, report_filename, report_size = _generate_report(
            load,
            report_df,
            template,
        )
        _generate_original_file(
            load,
            source_df,
            template,
            suffix,
        )
        logger.info(
            "Load %s generated report artifacts in %.3fs",
            load_id,
            perf_counter() - stage_started,
        )

        total_rows = len(validated_df.index)
        error_rows = total_rows - processed_rows

        final_status = (
            LOAD_STATUS_VALIDATED_SUCCESS
            if error_rows == 0
            else LOAD_STATUS_VALIDATED_WITH_ERRORS
        )
        finished_at = now_in_app_timezone()
        load = load_repo.update(
            Load(
                id=load.id,
                template_id=load.template_id,
                user_id=load.user_id,
                status=final_status,
                file_name=load.file_name,
                total_rows=total_rows,
                error_rows=error_rows,
                report_path=report_blob_path,
                created_at=load.created_at,
                started_at=load.started_at,
                finished_at=finished_at,
            )
        )

        _register_loaded_file(
            session,
            load_repo=load_repo,
            load=load,
            template=template,
            user=user,
            report_blob_path=report_blob_path,
            report_filename=report_filename,
            report_size=report_size,
        )
        notify_load_validated_success(
            session, load=load, template=template, user=user
        )
        logger.info(
            "Load %s finished end-to-end in %.3fs",
            load_id,
            perf_counter() - started_processing,
        )
    except Exception as exc:  # pragma: no cover - defensive path
        failed_load = _mark_load_as_failed(load_repo, load, str(exc))
        try:
            notify_load_status_changed(
                session, load=failed_load, template=template, user=user
            )
        except Exception:  # pragma: no cover - defensive path
            pass
        if isinstance(exc, (ValueError, PermissionError)):
            raise
        raise ValueError(str(exc)) from exc

    return load


def list_loads(
    session: Session,
    *,
    current_user: User,
    template_id: int | None = None,
    skip: int = 0,
    limit: int | None = 100,
) -> Sequence[Load]:
    """Return loads visible to ``current_user``."""

    repository = LoadRepository(session)
    if current_user.is_admin():
        return repository.list(
            template_id=template_id,
            creator_id=current_user.id,
            skip=skip,
            limit=limit,
        )
    return repository.list(
        template_id=template_id, user_id=current_user.id, skip=skip, limit=limit
    )


def list_loads_with_templates(
    session: Session,
    *,
    current_user: User,
    template_id: int | None = None,
    skip: int = 0,
    limit: int | None = None,
) -> Sequence[tuple[Load, Template, User]]:
    """Return loads and their templates visible to ``current_user``."""

    repository = LoadRepository(session)
    if current_user.is_admin():
        return repository.list_with_templates(
            template_id=template_id,
            creator_id=current_user.id,
            skip=skip,
            limit=limit,
        )
    return repository.list_with_templates(
        template_id=template_id,
        user_id=current_user.id,
        skip=skip,
        limit=limit,
    )


def get_load(
    session: Session,
    *,
    load_id: int,
    current_user: User,
) -> Load:
    """Return the load identified by ``load_id`` if accessible to ``current_user``."""

    repository = LoadRepository(session)
    load = repository.get(load_id)
    if load is None:
        raise ValueError("Carga no encontrada")
    if current_user.is_admin():
        if load.user_id == current_user.id:
            return load
        user_map = UserRepository(session).get_map_by_ids(
            [load.user_id], include_deleted=True
        )
        user = user_map.get(load.user_id)
        if user is None or user.created_by != current_user.id:
            raise PermissionError("No autorizado")
        return load
    if load.user_id != current_user.id:
        raise PermissionError("No autorizado")
    return load


def get_load_with_template(
    session: Session,
    *,
    load_id: int,
    current_user: User,
) -> tuple[Load, Template]:
    """Return the load along with its template details if accessible to the user."""

    repository = LoadRepository(session)
    result = repository.get_with_template(load_id)
    if result is None:
        raise ValueError("Carga no encontrada")
    load, template = result

    if current_user.is_admin():
        if load.user_id == current_user.id:
            return load, template
        user_map = UserRepository(session).get_map_by_ids(
            [load.user_id], include_deleted=True
        )
        user = user_map.get(load.user_id)
        if user is None or user.created_by != current_user.id:
            raise PermissionError("No autorizado")
        return load, template

    if load.user_id != current_user.id:
        raise PermissionError("No autorizado")

    return load, template


def get_load_report(
    session: Session,
    *,
    load_id: int,
    current_user: User,
) -> tuple[Load, Path, str]:
    """Return the filesystem path and display name for the report generated for ``load_id``."""

    load = get_load(session, load_id=load_id, current_user=current_user)
    template = _get_template(session, load.template_id)

    table_report = _build_temporary_table_report(load, template)
    if table_report is not None:
        path, filename = table_report
        return load, path, filename

    loaded_file = LoadedFileRepository(session).get_latest_by_load(load.id)
    if loaded_file is None:
        raise FileNotFoundError("Reporte no disponible para esta carga")

    if not current_user.is_admin() and loaded_file.created_user_id != current_user.id:
        raise PermissionError("No autorizado")

    blob_path = loaded_file.path or load.report_path
    if not blob_path:
        raise FileNotFoundError("Reporte no disponible para esta carga")
    try:
        path = _download_to_tempfile(blob_path, ".xlsx")
    except FileNotFoundError as exc:
        raise FileNotFoundError("Reporte no encontrado en el sistema de archivos") from exc
    download_name = loaded_file.name or _report_display_filename(template.name)
    return load, path, download_name


def get_load_original_file(
    session: Session,
    *,
    load_id: int,
    current_user: User,
) -> tuple[Load, Path, str]:
    """Return the original data file path and display name without extra columns for ``load_id``."""

    load = get_load(session, load_id=load_id, current_user=current_user)
    template = _get_template(session, load.template_id)

    temporary_original = _build_temporary_original_file(load, template)
    if temporary_original is not None:
        path, filename = temporary_original
        return load, path, filename

    suffix = Path(load.file_name).suffix.lower()
    extension = ".csv" if suffix == ".csv" else ".xlsx"

    if load.id is None:
        raise ValueError("Identificador de carga inválido")
    filename = _original_storage_filename(load.id, load.file_name, extension)
    blob_path = _build_report_blob_path(
        template.table_name,
        template.id,
        load.user_id,
        filename,
    )
    try:
        path = _download_to_tempfile(blob_path, extension)
    except FileNotFoundError as exc:
        raise FileNotFoundError("Archivo original no disponible para esta carga") from exc

    path = _clean_blob_original_file(path, extension)
    download_name = _original_download_name(load, extension)
    return load, path, download_name


def _clean_blob_original_file(path: Path, extension: str) -> Path:
    """Remove validation columns from the downloaded original file when possible."""

    try:
        pd = _get_pandas_module()
        if extension == ".csv":
            dataframe = pd.read_csv(path, dtype=object)
        else:
            dataframe = pd.read_excel(path, dtype=object)
    except Exception:  # pragma: no cover - fallback for unexpected formats
        return path

    cleaned = _prepare_original_dataframe(dataframe)
    if extension == ".csv":
        cleaned.to_csv(path, index=False, encoding="utf-8")
    else:
        buffer = BytesIO()
        cleaned.to_excel(buffer, index=False)
        buffer.seek(0)
        with path.open("wb") as handle:
            handle.write(buffer.getvalue())
    return path


def _get_template(session: Session, template_id: int) -> Template:
    template = TemplateRepository(session).get(template_id)
    if template is None:
        raise ValueError("Plantilla no encontrada")
    return template


def _ensure_user_has_access(session: Session, template: Template, user: User) -> None:
    if template.status != "published":
        raise PermissionError(
            "No es posible realizar cargas porque la plantilla debe estar publicada."
            " Comunícate con tu administrador."
        )
    if user.is_admin():
        return
    access = TemplateUserAccessRepository(session).get_active_access(
        user_id=user.id, template_id=template.id
    )
    if access is None:
        raise PermissionError("El usuario no tiene acceso a la plantilla solicitada")


def _ordered_active_columns(template: Template) -> list[TemplateColumn]:
    active_columns = [column for column in template.columns if column.is_active]
    return sorted(active_columns, key=lambda col: col.id or 0)


def _read_source_file(file_bytes: bytes, suffix: str) -> DataFrame:
    pd = _get_pandas_module()
    buffer = BytesIO(file_bytes)
    if suffix == ".csv":
        return pd.read_csv(buffer, dtype=object)
    return pd.read_excel(buffer, dtype=object)


def _normalize_dataframe(dataframe: DataFrame) -> DataFrame:
    pd = _get_pandas_module()
    pd.set_option('future.no_silent_downcasting', True)
    df = dataframe.copy()
    df.columns = [str(column).strip() for column in df.columns]
    def _replace_blank_strings(value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return pd.NA
        return value
    if len(df.columns):
        df = df.apply(lambda col: col.map(_replace_blank_strings) if col.dtype == "object" else col)
    df = df.infer_objects(copy=False)
    df.dropna(how="all", inplace=True)
    df = df.reset_index(drop=True)
    df = df.astype("object")
    return df


def _ensure_dataframe_has_rows(dataframe: DataFrame) -> None:
    if dataframe.empty:
        raise ValueError("El archivo proporcionado está en blanco")


def _validate_headers(dataframe: DataFrame, columns: Sequence[TemplateColumn]) -> None:
    expected = [column.name for column in columns]
    observed = list(dataframe.columns)
    if len(observed) != len(expected):
        raise ValueError(
            "El archivo no contiene la misma cantidad de columnas que la plantilla"
        )

    missing = [column for column in expected if column not in observed]
    extra = [column for column in observed if column not in expected]
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(
                "faltan columnas: " + ", ".join(f"'{name}'" for name in missing)
            )
        if extra:
            details.append(
                "hay columnas no esperadas: " + ", ".join(f"'{name}'" for name in extra)
            )
        raise ValueError("; ".join(details))

    if expected != observed:
        raise ValueError(
            "El orden de las columnas no coincide con la plantilla configurada"
        )


def _load_rules(
    session: Session, columns: Sequence[TemplateColumn]
) -> dict[int, dict[str, Any] | list[Any]]:
    rule_ids: set[int] = set()
    for column in columns:
        rule_ids.update(column.rule_ids)
    if not rule_ids:
        return {}
    repository = RuleRepository(session)
    return repository.get_active_map(tuple(rule_ids))


def _validate_dataframe(
    dataframe: DataFrame,
    columns: Sequence[TemplateColumn],
    rules: dict[int, dict[str, Any] | list[Any]],
) -> tuple[DataFrame, list[bool]]:
    pd = _get_pandas_module()
    df = dataframe.copy().astype("object")
    observations: list[list[str]] = [[] for _ in range(len(df.index))]
    row_is_valid = [True] * len(df.index)
    duplicate_rules: list[dict[str, Any]] = []
    seen_duplicate_configs: set[tuple[tuple[str, ...], str | None, str | None, bool]] = set()

    normalized_column_lookup: dict[str, str] = {}
    column_token_lookup: dict[str, tuple[str, ...]] = {}
    rule_header_lookup = _build_grouped_rule_header_lookup(columns, rules)

    for column in columns:
        normalized_name = _normalize_type_label(column.name)
        if normalized_name and normalized_name not in normalized_column_lookup:
            normalized_column_lookup[normalized_name] = column.name
        column_token_lookup[column.name] = _tokenize_label(column.name)
        if column.name not in df.columns:
            df[column.name] = pd.Series([None] * len(df.index), dtype=object)

    row_snapshots = df.to_dict(orient="records")

    for column in columns:
        normalized_type = _normalize_type_label(column.data_type)
        parser = _TYPE_PARSERS.get(normalized_type)
        column_rule_definitions = [
            (
                rules[assignment.id],
                rule_header_lookup.get((column.name, assignment.id), {}),
            )
            for assignment in column.rules
            if assignment.id in rules
        ]
        if parser is None and not column_rule_definitions:
            raise ValueError(
                f"Tipo de dato desconocido para la columna '{column.name}'"
            )

        series = df[column.name]
        if column_rule_definitions:
            for rule_definition, _rule_headers in column_rule_definitions:
                duplicate_configs = _gather_duplicate_rule_configs(
                    rule_definition,
                    column.name,
                    column_lookup=normalized_column_lookup,
                    column_tokens=column_token_lookup,
                )
                for config in duplicate_configs:
                    key = (
                        tuple(config["fields"]),
                        config.get("name"),
                        config.get("message"),
                        config.get("ignore_empty", False),
                    )
                    if key not in seen_duplicate_configs:
                        seen_duplicate_configs.add(key)
                        duplicate_rules.append(config)

        for idx, raw_value in enumerate(series.tolist()):
            normalized_value = _normalize_cell_value(raw_value)
            df.at[idx, column.name] = normalized_value
            row_snapshot = row_snapshots[idx]
            row_snapshot[column.name] = normalized_value

            column_errors: list[str] = []
            converted_value = normalized_value

            if column_rule_definitions:
                current_value = normalized_value
                for rule_definition, rule_headers in column_rule_definitions:
                    candidate_value, rule_errors = _validate_value_with_rule(
                        column.name,
                        current_value,
                        rule_definition,
                        row_snapshot,
                        parser,
                        normalized_column_lookup,
                        column_token_lookup,
                        rule_headers,
                    )
                    if rule_errors:
                        column_errors.extend(rule_errors)
                        continue

                    current_value = candidate_value
                    row_snapshot[column.name] = current_value

                if not column_errors:
                    converted_value = current_value
            elif parser is not None:
                parsed_value, parse_error = parser(normalized_value)
                if parse_error:
                    column_errors.append(f"{column.name}: {parse_error}")
                else:
                    converted_value = parsed_value

            if column_errors:
                observations[idx].extend(column_errors)
                row_is_valid[idx] = False
            else:
                df.at[idx, column.name] = converted_value
                row_snapshot[column.name] = converted_value

    if duplicate_rules:
        _apply_duplicate_rules(df, duplicate_rules, observations, row_is_valid)

    df["observaciones"] = [
        _summarize_observations(messages) for messages in observations
    ]
    df["status"] = [
        "Procesado" if is_valid else "No procesado" for is_valid in row_is_valid
    ]

    ordered_columns = [
        column
        for column in df.columns
        if column not in {"status", "observaciones"}
    ]
    ordered_columns.extend(["status", "observaciones"])
    df = df.loc[:, ordered_columns]

    return df, row_is_valid


def _normalize_cell_value(value: Any) -> Any:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` converted to ``int`` when safe, otherwise ``None``."""

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        try:
            integral = value.to_integral_value()
        except InvalidOperation:
            return None
        if value == integral:
            return int(integral)
        return None
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            return int(candidate)
        except ValueError:
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _summarize_observations(messages: Sequence[str]) -> str:
    if not messages:
        return ""

    summary: list[str] = []
    seen: set[str] = set()
    for raw_message in messages:
        simplified = _simplify_error_message(raw_message)
        if simplified and simplified not in seen:
            seen.add(simplified)
            summary.append(simplified)
    return " | ".join(summary)


def _simplify_error_message(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)

    if "(" in text and text.endswith(")"):
        prefix, suffix = text.rsplit("(", 1)
        candidate = suffix.rstrip(")").strip()
        if candidate and not prefix.strip():
            text = candidate
        elif candidate:
            text = prefix.strip() or candidate

    if ":" in text:
        label, detail = text.split(":", 1)
        label = label.strip()
        detail = detail.strip()
        if label and detail:
            return f"{label}: {detail}"
        return label or detail

    return text


def _persist_rows(
    dataframe: DataFrame,
    row_is_valid: Sequence[bool],
    table_name: str,
    *,
    operation_number: int,
    column_identifier_map: Mapping[str, str] | None = None,
) -> int:
    pd = _get_pandas_module()
    if not len(dataframe.index):
        return 0

    working_df = dataframe.copy()
    working_df["numero_operacion"] = operation_number
    normalized_df = working_df.where(pd.notnull(working_df), None)
    identifier_map = column_identifier_map or {}
    records = []
    for row in normalized_df.to_dict(orient="records"):
        normalized_row: dict[str, Any] = {}
        for key, value in row.items():
            target_key = identifier_map.get(key, key)
            if isinstance(value, pd.Timestamp):
                normalized_row[target_key] = _coerce_timestamp(value)
            elif value is None:
                normalized_row[target_key] = None
            else:
                normalized_row[target_key] = value
        records.append(normalized_row)

    metadata = MetaData()
    table = Table(table_name, metadata, autoload_with=engine)
    sanitized_records: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        is_valid = row_is_valid[index] if index < len(row_is_valid) else False
        sanitized_records.append(
            _sanitize_record_for_insert(record, table=table, row_is_valid=is_valid)
        )
    try:
        with engine.begin() as connection:
            connection.execute(insert(table), sanitized_records)
    except SQLAlchemyError as exc:  # pragma: no cover - defensive passthrough
        raise RuntimeError(
            f"No se pudo insertar la información validada en la tabla '{table_name}'"
        ) from exc

    return sum(1 for flag in row_is_valid if flag)


def _sanitize_record_for_insert(
    record: Mapping[str, Any], *, table: Table, row_is_valid: bool
) -> dict[str, Any]:
    sanitized = dict(record)
    if row_is_valid:
        return sanitized

    for key, value in list(sanitized.items()):
        if key not in table.c:
            continue
        if key in {"id", "numero_operacion", "status", "observaciones"}:
            continue
        sanitized[key] = _coerce_value_for_column(value, table.c[key].type)
    return sanitized


def _coerce_value_for_column(value: Any, column_type: Any) -> Any:
    if value is None:
        return None

    parser: Callable[[Any], tuple[Any, str | None]] | None = None
    if isinstance(column_type, Integer):
        parser = _parse_integer
    elif isinstance(column_type, (Float, Numeric)):
        parser = _parse_float
    elif isinstance(column_type, Boolean):
        parser = _parse_boolean
    elif isinstance(column_type, DateTime):
        parser = _parse_datetime
    elif isinstance(column_type, Date):
        parser = _parse_date

    if parser is None:
        return value

    parsed_value, parse_error = parser(value)
    if parse_error:
        return None
    return parsed_value


def _generate_report(
    load: Load, dataframe: DataFrame, template: Template
) -> tuple[str, str, int]:
    if load.id is None:
        raise ValueError("Identificador de carga inválido")
    display_filename = _report_display_filename(template.name)
    storage_filename = _report_storage_filename(load.id, template.name)
    blob_path = _build_report_blob_path(
        template.table_name,
        load.template_id,
        load.user_id,
        storage_filename,
    )
    buffer = BytesIO()
    dataframe.to_excel(buffer, index=False)
    data = buffer.getvalue()
    upload_blob(blob_path, data, content_type=_EXCEL_CONTENT_TYPE)
    return blob_path, display_filename, len(data)


def _prepare_report_dataframe(
    dataframe: DataFrame, *, operation_number: int | None = None
) -> DataFrame:
    df = dataframe.copy()

    if operation_number is not None:
        df["numero_operacion"] = operation_number

    if "status" not in df.columns:
        df["status"] = ""
    if "observaciones" not in df.columns:
        df["observaciones"] = ""

    ordered_columns: list[str] = []
    if "numero_operacion" in df.columns:
        ordered_columns.append("numero_operacion")

    ordered_columns.extend(
        column
        for column in df.columns
        if column
        not in {"numero_operacion", "status", "observaciones"}
    )
    ordered_columns.extend(["status", "observaciones"])
    return df.loc[:, ordered_columns]


def _fetch_temporary_table_dataframe(
    template: Template, *, operation_number: int
) -> DataFrame | None:
    pd = _get_pandas_module()
    metadata = MetaData()
    try:
        table = Table(template.table_name, metadata, autoload_with=engine)
    except SQLAlchemyError:  # pragma: no cover - passthrough for db layer
        return None

    if "numero_operacion" not in table.c:
        return None

    active_columns = _ordered_active_columns(template)
    identifier_map = {
        derive_column_identifier(column.name): column.name for column in active_columns
    }

    select_columns: list[Any] = []
    if "numero_operacion" in table.c:
        select_columns.append(table.c.numero_operacion)

    for identifier in identifier_map:
        if identifier in table.c and table.c[identifier] not in select_columns:
            select_columns.append(table.c[identifier])

    for extra in ("status", "observaciones"):
        if extra in table.c and table.c[extra] not in select_columns:
            select_columns.append(table.c[extra])

    if not select_columns:
        return None

    statement = (
        select(*select_columns)
        .where(table.c.numero_operacion == operation_number)
        .order_by(table.c.id)
    )
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()

    if rows:
        df = pd.DataFrame(rows)
    else:
        df = pd.DataFrame(columns=[column.key for column in select_columns])

    rename_map = {
        identifier: display
        for identifier, display in identifier_map.items()
        if identifier in df.columns
    }
    df = df.rename(columns=rename_map)

    for extra in ("status", "observaciones"):
        if extra not in df.columns:
            df[extra] = ""

    desired_order: list[str] = []

    if "numero_operacion" in df.columns:
        desired_order.append("numero_operacion")

    desired_order.extend(
        column.name for column in active_columns if column.name in df.columns
    )
    for extra in ("status", "observaciones"):
        if extra in df.columns and extra not in desired_order:
            desired_order.append(extra)
    if desired_order:
        df = df.loc[:, desired_order]

    return df


def _build_temporary_table_report(
    load: Load, template: Template
) -> tuple[Path, str] | None:
    if load.id is None:
        return None

    dataframe = _fetch_temporary_table_dataframe(
        template, operation_number=load.id
    )
    if dataframe is None or not len(dataframe.columns):
        return None

    _DOWNLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    buffer = BytesIO()
    dataframe.to_excel(buffer, index=False)
    buffer.seek(0)
    destination = _DOWNLOAD_DIRECTORY / f"{uuid4().hex}.xlsx"
    with destination.open("wb") as handle:
        handle.write(buffer.getvalue())

    filename = _report_display_filename(template.name)
    return destination, filename


def _build_temporary_original_file(
    load: Load, template: Template
) -> tuple[Path, str] | None:
    if load.id is None:
        return None

    dataframe = _fetch_temporary_table_dataframe(
        template, operation_number=load.id
    )
    if dataframe is None or not len(dataframe.columns):
        return None

    clean_df = _prepare_original_dataframe(dataframe)
    suffix = Path(load.file_name).suffix.lower()
    extension = ".csv" if suffix == ".csv" else ".xlsx"

    _DOWNLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = _DOWNLOAD_DIRECTORY / f"{uuid4().hex}{extension}"

    if extension == ".csv":
        clean_df.to_csv(destination, index=False, encoding="utf-8")
    else:
        buffer = BytesIO()
        clean_df.to_excel(buffer, index=False)
        buffer.seek(0)
        with destination.open("wb") as handle:
            handle.write(buffer.getvalue())

    filename = _original_download_name(load, extension)
    return destination, filename


def _prepare_original_dataframe(dataframe: DataFrame) -> DataFrame:
    removable = {
        "status",
        "observaciones",
        "Status",
        "Observaciones",
        "numero_operacion",
    }
    drop_columns = [column for column in removable if column in dataframe.columns]
    return dataframe.drop(columns=drop_columns, errors="ignore")


def _generate_original_file(
    load: Load, dataframe: DataFrame, template: Template, suffix: str
) -> str:
    if load.id is None:
        raise ValueError("Identificador de carga inválido")
    extension = ".csv" if suffix == ".csv" else ".xlsx"
    filename = _original_storage_filename(load.id, load.file_name, extension)
    blob_path = _build_report_blob_path(
        template.table_name,
        load.template_id,
        load.user_id,
        filename,
    )

    if extension == ".csv":
        buffer = StringIO()
        dataframe.to_csv(buffer, index=False)
        data = buffer.getvalue().encode("utf-8")
        content_type = _CSV_CONTENT_TYPE
    else:
        buffer = BytesIO()
        dataframe.to_excel(buffer, index=False)
        data = buffer.getvalue()
        content_type = _EXCEL_CONTENT_TYPE

    upload_blob(blob_path, data, content_type=content_type)
    return blob_path


def _register_loaded_file(
    session: Session,
    *,
    load_repo: LoadRepository,
    load: Load,
    template: Template,
    user: User,
    report_blob_path: str,
    report_filename: str,
    report_size: int,
) -> None:
    loaded_file_repo = LoadedFileRepository(session)
    num_load = load_repo.count_completed_by_user_and_template(
        user_id=user.id, template_id=template.id
    )
    loaded_file_repo.create(
        LoadedFile(
            id=None,
            load_id=load.id,
            name=report_filename,
            path=report_blob_path,
            size_bytes=report_size,
            num_load=num_load,
            created_user_id=user.id,
            created_at=None,
        )
    )


def _mark_load_as_failed(
    repository: LoadRepository, load: Load, message: str
) -> Load:
    finished_at = now_in_app_timezone()
    return repository.update(
        Load(
            id=load.id,
            template_id=load.template_id,
            user_id=load.user_id,
            status=LOAD_STATUS_FAILED,
            file_name=load.file_name,
            total_rows=load.total_rows,
            error_rows=load.error_rows,
            report_path=load.report_path,
            created_at=load.created_at,
            started_at=load.started_at or finished_at,
            finished_at=finished_at,
        )
    )


def _validate_value_with_rule(
    column_name: str,
    value: Any,
    rule_definition: dict[str, Any] | list[Any],
    row_context: Mapping[str, Any],
    base_parser: Callable[[Any], tuple[Any, str | None]] | None,
    column_lookup: Mapping[str, str],
    column_tokens: Mapping[str, tuple[str, ...]],
    rule_headers: Mapping[str, str] | None = None,
) -> tuple[Any, list[str]]:
    if isinstance(rule_definition, list):
        current_value = value
        all_errors: list[str] = []
        for definition in rule_definition:
            current_value, definition_errors = _validate_value_with_rule(
                column_name,
                current_value,
                definition,
                row_context,
                base_parser,
                column_lookup,
                column_tokens,
                rule_headers,
            )
            if definition_errors:
                all_errors.extend(definition_errors)
        return current_value, all_errors

    if not isinstance(rule_definition, dict):
        return value, [f"{column_name}: configuración de regla inválida"]

    message = rule_definition.get("Mensaje de error")
    normalized_message = message.strip() if isinstance(message, str) else None
    required = bool(rule_definition.get("Campo obligatorio"))

    if value is None:
        if required:
            return None, [
                _compose_error(
                    normalized_message,
                    "es obligatorio",
                    column_name=column_name,
                    cell_value=value,
                )
            ]
        return None, []

    tipo = rule_definition.get("Tipo de dato")
    normalized_tipo = _normalize_type_label(tipo or "")
    rule_config = rule_definition.get("Regla")
    if not isinstance(rule_config, dict):
        rule_config = {}

    validator = _RULE_VALIDATORS.get(normalized_tipo)
    if validator is None:
        parsed_value, parse_errors = _apply_base_parser(
            value, column_name, base_parser, normalized_message
        )
        return parsed_value, parse_errors

    parsed_value, validator_errors = validator(
        value=value,
        column_name=column_name,
        rule_definition=rule_definition,
        rule_config=rule_config,
        message=normalized_message,
        row_context=row_context,
        base_parser=base_parser,
        column_lookup=column_lookup,
        column_tokens=column_tokens,
        rule_headers=rule_headers,
    )
    return parsed_value, validator_errors


def _gather_duplicate_rule_configs(
    rule_definition: Any,
    column_name: str,
    *,
    column_lookup: Mapping[str, str],
    column_tokens: Mapping[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    if isinstance(rule_definition, list):
        configs: list[dict[str, Any]] = []
        for definition in rule_definition:
            configs.extend(
                _gather_duplicate_rule_configs(
                    definition,
                    column_name,
                    column_lookup=column_lookup,
                    column_tokens=column_tokens,
                )
            )
        return configs

    if not isinstance(rule_definition, Mapping):
        return []

    rule_type = _normalize_type_label(rule_definition.get("Tipo de dato", ""))
    if rule_type != "duplicados":
        return []

    message = rule_definition.get("Mensaje de error")
    normalized_message = None
    if isinstance(message, str):
        stripped_message = message.strip()
        if stripped_message:
            normalized_message = stripped_message

    rule_name = rule_definition.get("Nombre de la regla")
    normalized_name = None
    if isinstance(rule_name, str):
        stripped_name = rule_name.strip()
        if stripped_name:
            normalized_name = stripped_name

    raw_config = rule_definition.get("Regla")
    config: Mapping[str, Any] = raw_config if isinstance(raw_config, Mapping) else {}

    candidate_keys = ["Campos", "Columnas", "Fields", "fields"]
    fields: list[str] = []
    for key in candidate_keys:
        raw_fields = config.get(key)
        if isinstance(raw_fields, Sequence) and not isinstance(raw_fields, (str, bytes)):
            for field in raw_fields:
                if isinstance(field, str):
                    normalized = field.strip()
                    if normalized:
                        fields.append(normalized)
            if fields:
                break

    if not fields and column_name:
        fields = [column_name]

    unique_fields: list[str] = []
    seen_fields: set[str] = set()
    for field in fields:
        if field not in seen_fields:
            seen_fields.add(field)
            unique_fields.append(field)

    if not unique_fields:
        return []

    matched_fields, missing_fields = _resolve_duplicate_field_matches(
        unique_fields, column_lookup, column_tokens
    )

    if missing_fields:
        missing_str = ", ".join(sorted(set(missing_fields)))
        rule_label = normalized_name or rule_name or column_name or "Regla de duplicados"
        raise ValueError(
            f"La regla de duplicados '{rule_label}' hace referencia a columnas inexistentes: {missing_str}"
        )

    resolved_fields = matched_fields or unique_fields

    ignore_empty = bool(
        config.get("Ignorar vacios")
        or config.get("Ignorar vacíos")
        or config.get("Ignorar vacias")
        or config.get("Ignorar vacías")
        or config.get("Ignore empty")
        or config.get("Ignore empties")
    )

    return [
        {
            "fields": resolved_fields,
            "message": normalized_message,
            "name": normalized_name,
            "ignore_empty": ignore_empty,
        }
    ]


def _resolve_duplicate_field_matches(
    fields: Sequence[str],
    column_lookup: Mapping[str, str],
    column_tokens: Mapping[str, tuple[str, ...]],
) -> tuple[list[str], list[str]]:
    matched: list[str] = []
    missing: list[str] = []
    used: set[str] = set()

    for field in fields:
        normalized = _normalize_type_label(field)
        candidate: str | None = None

        if normalized and normalized in column_lookup:
            candidate = column_lookup[normalized]
        else:
            candidate = _find_best_column_match(field, column_tokens)

        if candidate is None:
            missing.append(field)
            continue

        if candidate not in used:
            matched.append(candidate)
            used.add(candidate)

    return matched, missing


def _find_best_column_match(
    field: str, column_tokens: Mapping[str, tuple[str, ...]]
) -> str | None:
    tokens = _tokenize_label(field)
    token_set = frozenset(tokens)
    normalized_field = _normalize_type_label(field)

    best_score = 0.0
    best_candidate: str | None = None

    for column_name, candidate_tokens in column_tokens.items():
        normalized_candidate = _normalize_type_label(column_name)

        if normalized_field and normalized_field == normalized_candidate:
            return column_name
        if tokens and tokens == candidate_tokens:
            return column_name

        score = 0.0
        candidate_token_set = frozenset(candidate_tokens)
        if token_set and candidate_token_set:
            intersection = len(token_set & candidate_token_set)
            if intersection:
                score = intersection / max(len(token_set), len(candidate_token_set))

        if normalized_field:
            ratio = SequenceMatcher(None, normalized_field, normalized_candidate).ratio()
            score = max(score, ratio)

        if score > best_score:
            best_score = score
            best_candidate = column_name

    if best_candidate and best_score >= 0.6:
        return best_candidate
    return None


def _apply_base_parser(
    value: Any,
    column_name: str,
    base_parser: Callable[[Any], tuple[Any, str | None]] | None,
    message: str | None,
    *,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
) -> tuple[Any, list[str]]:
    if base_parser is None:
        return value, []
    parsed_value, parse_error = base_parser(value)
    if parse_error:
        return value, [
            _compose_error(
                message,
                parse_error,
                column_name=column_name,
                cell_value=value,
                dependent_field=dependent_field,
                dependent_value=dependent_value,
            )
        ]
    return parsed_value, []


def _format_cell_display(value: Any) -> str:
    if value is None:
        return "vacío"
    if isinstance(value, float):
        try:
            if math.isnan(value):
                return "vacío"
        except TypeError:
            pass
    if isinstance(value, str):
        if not value:
            return "vacío"
        sanitized = value.replace("\"", "'")
        return f'"{sanitized}"'
    if isinstance(value, (list, dict)):
        try:
            serialized = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = str(value)
        sanitized = serialized.replace("\"", "'")
        return f'"{sanitized}"'
    representation = str(value)
    if not representation:
        return "vacío"
    sanitized = representation.replace("\"", "'")
    return f'"{sanitized}"'


def _compose_column_error_detail(
    column_name: str,
    cell_value: Any,
    reason: str,
    *,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    invalid_values: Sequence[str] | None = None,
) -> str:
    normalized_reason = str(reason or "").strip()

    cell_display = _format_cell_display(cell_value)

    if invalid_values:
        values_text = ", ".join(invalid_values)
        base_detail = f"La combinación de valores {values_text} no es válida"
        normalized_reason = ""
    elif dependent_field:
        dependent_display = _format_cell_display(dependent_value)
        base_detail = (
            f'La columna "{column_name}" con el valor {cell_display} '
            f"no es válida cuando \"{dependent_field}\" tiene el valor {dependent_display}"
        )
    else:
        base_detail = (
            f'La columna "{column_name}" con el valor {cell_display} '
            "no cumple con la validación"
        )

    if normalized_reason:
        normalized_reason = normalized_reason.rstrip(".")
        base_detail = f"{base_detail}: {normalized_reason}."
    else:
        if not base_detail.endswith("."):
            base_detail = f"{base_detail}."

    return f"{base_detail} Revisa la configuración en Accura de la plantilla."


def _compose_error(
    message: str | None,
    fallback: str,
    *,
    column_name: str | None = None,
    cell_value: Any | None = None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    invalid_values: Sequence[str] | None = None,
) -> str:
    if column_name is not None:
        detail = _compose_column_error_detail(
            column_name,
            cell_value,
            fallback,
            dependent_field=dependent_field,
            dependent_value=dependent_value,
            invalid_values=invalid_values,
        )
    else:
        detail = fallback
    # Actualmente se solicita ignorar los mensajes de error personalizados y
    # mostrar siempre el mensaje del sistema.
    # if message:
    #     return message
    return detail


def _apply_duplicate_rules(
    dataframe: DataFrame,
    duplicate_rules: Sequence[Mapping[str, Any]],
    observations: list[list[str]],
    row_is_valid: list[bool],
) -> None:
    if not duplicate_rules or not len(dataframe.index):
        return

    for rule in duplicate_rules:
        raw_fields = rule.get("fields")
        if not isinstance(raw_fields, Sequence):
            continue
        fields = [field for field in raw_fields if isinstance(field, str) and field]
        if not fields:
            continue

        missing_fields = [field for field in fields if field not in dataframe.columns]
        if missing_fields:
            detail = (
                f"{rule.get('name') or 'Regla de duplicados'}: "
                f"campos no encontrados ({', '.join(missing_fields)})"
            )
            for idx in range(len(observations)):
                observations[idx].append(detail)
                row_is_valid[idx] = False
            continue

        subset = dataframe[fields]
        duplicated_mask = subset.duplicated(keep=False)
        if not duplicated_mask.any():
            continue

        ignore_empty = bool(rule.get("ignore_empty"))
        if ignore_empty:
            non_empty_mask = subset.notna().any(axis=1)
            duplicated_mask = duplicated_mask & non_empty_mask

        if not duplicated_mask.any():
            continue

        message = rule.get("message") if isinstance(rule.get("message"), str) else None
        for idx in subset.index[duplicated_mask]:
            values = "; ".join(
                f"{field}={_format_duplicate_value(dataframe.at[idx, field])}"
                for field in fields
            )
            fallback = (
                f"Registros duplicados en campos {', '.join(fields)} ({values}). "
                "Revisa la configuración en Accura de la plantilla para la regla de duplicados."
            )
            observations[idx].append(_compose_error(message, fallback))
            row_is_valid[idx] = False


def _format_duplicate_value(value: Any) -> str:
    if value is None:
        return "vacío"
    return str(value)


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


def _labels_match(raw_header: str, expected_normalized: str, expected_label: str | None) -> bool:
    normalized_header = _normalize_type_label(raw_header)
    if normalized_header == expected_normalized:
        return True
    if expected_label is None:
        return False
    return _tokenize_label(raw_header) == _tokenize_label(expected_label)


def _labels_equivalent(
    normalized_candidate: str,
    raw_candidate: str,
    expected_normalized: str,
    expected_label: str | None,
) -> bool:
    if normalized_candidate == expected_normalized:
        return True
    if expected_label is None:
        return False
    return _tokenize_label(raw_candidate) == _tokenize_label(expected_label)


def _validate_text_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    text_value = _stringify_scalar(value)
    errors: list[str] = []
    min_length = _coerce_int(rule_config.get("Longitud mínima"))
    max_length = _coerce_int(rule_config.get("Longitud máxima"))
    length = len(text_value)
    if min_length is not None and max_length is not None:
        if length < min_length or length > max_length:
            errors.append(
                _compose_error(
                    message,
                    f"longitud entre {min_length} y {max_length} caracteres",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            )
    else:
        if min_length is not None and length < min_length:
            errors.append(
                _compose_error(
                    message,
                    f"longitud mínima {min_length} caracteres",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            )
        if max_length is not None and length > max_length:
            errors.append(
                _compose_error(
                    message,
                    f"longitud máxima {max_length} caracteres",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            )
    return text_value, errors


def _validate_document_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    text_value = _stringify_scalar(value)
    errors: list[str] = []
    min_length = _coerce_int(rule_config.get("Longitud mínima"))
    max_length = _coerce_int(rule_config.get("Longitud máxima"))
    length = len(text_value)
    if min_length is not None and max_length is not None:
        if length < min_length or length > max_length:
            errors.append(
                _compose_error(
                    message,
                    f"longitud entre {min_length} y {max_length} caracteres",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            )
    else:
        if min_length is not None and length < min_length:
            errors.append(
                _compose_error(
                    message,
                    f"longitud mínima {min_length} caracteres",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            )
        if max_length is not None and length > max_length:
            errors.append(
                _compose_error(
                    message,
                    f"longitud máxima {max_length} caracteres",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            )
    return text_value, errors


def _validate_number_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[Any, list[str]]:
    errors: list[str] = []
    try:
        numeric_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        errors.append(
            _compose_error(
                message,
                "debe ser numérico",
                column_name=column_name,
                cell_value=value,
                dependent_field=dependent_field,
                dependent_value=dependent_value,
            )
        )
        return value, errors

    decimals_allowed = rule_config.get("Número de decimales")
    if isinstance(decimals_allowed, int):
        actual_decimals = max(-numeric_value.as_tuple().exponent, 0)
        if actual_decimals > decimals_allowed:
            errors.append(
                _compose_error(
                    message,
                    f"máximo {decimals_allowed} decimales",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            )

    min_value = rule_config.get("Valor mínimo")
    if min_value is not None:
        try:
            if numeric_value < Decimal(str(min_value)):
                errors.append(
                    _compose_error(
                        message,
                        f"valor mínimo {min_value}",
                        column_name=column_name,
                        cell_value=value,
                        dependent_field=dependent_field,
                        dependent_value=dependent_value,
                    )
                )
        except (InvalidOperation, ValueError):
            errors.append(
                _compose_error(
                    message,
                    "límite mínimo inválido",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                ),
            )

    max_value = rule_config.get("Valor máximo")
    if max_value is not None:
        try:
            if numeric_value > Decimal(str(max_value)):
                errors.append(
                    _compose_error(
                        message,
                        f"valor máximo {max_value}",
                        column_name=column_name,
                        cell_value=value,
                        dependent_field=dependent_field,
                        dependent_value=dependent_value,
                    )
                )
        except (InvalidOperation, ValueError):
            errors.append(
                _compose_error(
                    message,
                    "límite máximo inválido",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                ),
            )

    if errors:
        return value, errors

    if isinstance(decimals_allowed, int) and decimals_allowed == 0:
        return int(numeric_value), []
    return float(numeric_value), []


def _validate_list_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    text_value = str(value)
    allowed_values = _extract_allowed_values(rule_config)
    if not allowed_values:
        return text_value, []
    normalized_choices: dict[str | None, str] = {}
    for choice in allowed_values:
        normalized_choice = _normalize_choice_value(choice)
        display_value = "" if choice is None else str(choice)
        if normalized_choice not in normalized_choices:
            normalized_choices[normalized_choice] = display_value

    if not normalized_choices:
        return text_value, []

    normalized_value = _normalize_choice_value(text_value)
    if normalized_value not in normalized_choices:
        allowed_display = sorted(
            {display for display in normalized_choices.values()},
            key=lambda item: unicodedata.normalize("NFKC", item).casefold(),
        )
        return text_value, [
            _compose_error(
                message,
                f"valor no permitido ({', '.join(allowed_display)})",
                column_name=column_name,
                cell_value=value,
                dependent_field=dependent_field,
                dependent_value=dependent_value,
            )
        ]
    canonical_value = normalized_choices.get(normalized_value, text_value)
    return canonical_value, []


def _resolve_row_field_reference(
    field: str,
    *,
    row_context: Mapping[str, Any],
    column_lookup: Mapping[str, str] | None,
    column_tokens: Mapping[str, tuple[str, ...]] | None,
    rule_headers: Mapping[str, str] | None = None,
) -> str | None:
    normalized_field = _normalize_type_label(field)
    if not normalized_field:
        return None

    if rule_headers:
        mapped = rule_headers.get(normalized_field)
        if mapped and mapped in row_context:
            return mapped

    for key in row_context:
        if isinstance(key, str) and _labels_match(key, normalized_field, field):
            return key

    if column_lookup:
        mapped = column_lookup.get(normalized_field)
        if mapped and mapped in row_context:
            return mapped

    if column_tokens:
        target_tokens = _tokenize_label(field)
        if target_tokens:
            matches = [
                header
                for header, tokens in column_tokens.items()
                if header in row_context and tokens and tokens == target_tokens
            ]
            if not matches:
                target_set = set(target_tokens)
                matches = [
                    header
                    for header, tokens in column_tokens.items()
                    if header in row_context
                    and tokens
                    and (
                        target_set.issubset(set(tokens))
                        or set(tokens).issubset(target_set)
                    )
                ]
            if len(matches) == 1:
                return matches[0]

    return None


def _validate_full_list_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    row_context: Mapping[str, Any] | None,
    column_lookup: Mapping[str, str] | None = None,
    column_tokens: Mapping[str, tuple[str, ...]] | None = None,
    rule_headers: Mapping[str, str] | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    text_value = str(value)
    combinations = _extract_composite_combinations(rule_config)
    if not combinations:
        return text_value, []

    row_snapshot: Mapping[str, Any] = row_context or {}
    related_fields: set[str] = {column_name}

    resolver_cache: dict[str, str | None] = {}
    resolved_combinations: list[dict[str, str]] = []
    for combination in combinations:
        resolved_entry: dict[str, str] = {}
        for field, expected in combination.items():
            if field in resolver_cache:
                resolved_field = resolver_cache[field]
            else:
                resolved_field = _resolve_row_field_reference(
                    field,
                    row_context=row_snapshot,
                    column_lookup=column_lookup,
                    column_tokens=column_tokens,
                    rule_headers=rule_headers,
                )
                resolver_cache[field] = resolved_field

            target_field = resolved_field or field
            related_fields.add(target_field)
            resolved_entry[target_field] = expected
        if resolved_entry:
            resolved_combinations.append(resolved_entry)

    if not resolved_combinations:
        return text_value, []

    normalized_combinations: list[dict[str, str | None]] = [
        {
            field: _normalize_choice_value(expected)
            for field, expected in combination.items()
        }
        for combination in resolved_combinations
    ]

    def _stringify(raw: Any) -> str | None:
        normalized = _normalize_cell_value(raw)
        if normalized is None:
            return None
        return str(normalized)

    current_values: dict[str, str | None] = {}
    normalized_current_values: dict[str, str | None] = {}
    for field in sorted(related_fields):
        raw_value = value if field == column_name else row_snapshot.get(field)
        text_related = _stringify(raw_value)
        current_values[field] = text_related
        normalized_current_values[field] = _normalize_choice_value(text_related)

    def _compose_invalid_pairs(fields: Sequence[str]) -> list[str]:
        pairs: list[str] = []
        for field in fields:
            current_value = current_values.get(field)
            if current_value in (None, ""):
                display_value = "(vacío)"
            else:
                display_value = str(current_value)
            pairs.append(f"{field} = {display_value}")
        return pairs
    missing_for_viable: set[str] = set()
    for combination, normalized_combination in zip(
        resolved_combinations, normalized_combinations
    ):
        missing_fields = [field for field in combination if current_values.get(field) is None]
        if missing_fields:
            if all(
                normalized_current_values.get(field) == normalized_combination.get(field)
                for field in combination
                if field not in missing_fields
            ):
                missing_for_viable.update(missing_fields)
            continue
        if all(
            normalized_current_values.get(field) == normalized_combination.get(field)
            for field in combination
        ):
            return text_value, []

    if missing_for_viable:
        missing = ", ".join(sorted(missing_for_viable))
        return text_value, [
            _compose_error(
                message,
                f"completa los campos relacionados ({missing})",
                column_name=column_name,
                cell_value=value,
                invalid_values=_compose_invalid_pairs(sorted(field for field in related_fields if field is not None)),
            )
        ]

    sorted_fields = sorted(field for field in related_fields if field is not None)
    summary = "; ".join(
        " / ".join(f"{field}={expected}" for field, expected in combination.items())
        for combination in resolved_combinations
    )

    if sorted_fields:
        detailed_values = []
        for field in sorted_fields:
            current_value = current_values.get(field)
            if current_value in (None, ""):
                display_value = "(vacío)"
            else:
                display_value = str(current_value)
            detailed_values.append(f"{field} : {display_value}")
        combination_details = " , ".join(detailed_values)
        reason = f"la combinación {combination_details} no es válida"
    else:
        columns_summary = ", ".join(sorted_fields)
        if columns_summary:
            reason = f"combinación no permitida entre {columns_summary}"
            if summary:
                reason = f"{reason} ({summary})"
        else:
            reason = "combinación no permitida"
            if summary:
                reason = f"{reason} ({summary})"
    return text_value, [
        _compose_error(
            None,
            reason,
            column_name=column_name,
            cell_value=value,
            invalid_values=_compose_invalid_pairs(sorted_fields),
        )
    ]


def _validate_phone_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    text_value = str(value)
    digits = re.sub(r"[^0-9]", "", text_value)
    if not digits:
        return text_value, [
            _compose_error(
                message,
                "debe contener solo números",
                column_name=column_name,
                cell_value=value,
                dependent_field=dependent_field,
                dependent_value=dependent_value,
            ),
        ]

    country_code = str(rule_config.get("Código de país") or "")
    code_digits = re.sub(r"[^0-9]", "", country_code)
    local_digits = digits
    if code_digits:
        if not digits.startswith(code_digits):
            return text_value, [
                _compose_error(
                    message,
                    f"debe iniciar con el código de país {country_code}",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            ]
        local_digits = digits[len(code_digits) :]

    min_length = _coerce_int(rule_config.get("Longitud mínima"))
    max_length = _coerce_int(rule_config.get("Longitud máxima"))
    length = len(local_digits)
    if min_length is not None and max_length is not None:
        if length < min_length or length > max_length:
            return text_value, [
                _compose_error(
                    message,
                    f"longitud entre {min_length} y {max_length} dígitos",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            ]
    else:
        if min_length is not None and length < min_length:
            return text_value, [
                _compose_error(
                    message,
                    f"longitud mínima de {min_length} dígitos",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            ]
        if max_length is not None and length > max_length:
            return text_value, [
                _compose_error(
                    message,
                    f"longitud máxima de {max_length} dígitos",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                )
            ]

    formatted = f"+{code_digits}{local_digits}" if code_digits else local_digits
    return formatted, []


def _validate_email_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    text_value = str(value)
    pattern = rule_config.get("Formato")
    if isinstance(pattern, str) and pattern.strip():
        if not re.fullmatch(pattern, text_value):
            return text_value, [
                _compose_error(
                    message,
                    "formato de correo inválido",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                ),
            ]

    max_length = _coerce_int(rule_config.get("Longitud máxima"))
    if max_length is not None and len(text_value) > max_length:
        return text_value, [
            _compose_error(
                message,
                f"longitud máxima {max_length} caracteres",
                column_name=column_name,
                cell_value=value,
                dependent_field=dependent_field,
                dependent_value=dependent_value,
            )
        ]

    return text_value.lower(), []


def _validate_date_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[date, list[str]]:
    pd = _get_pandas_module()
    if isinstance(value, datetime):
        return value.date(), []

    format_alias = rule_config.get("Formato")
    format_map = {
        "yyyy-mm-dd": "%Y-%m-%d",
        "dd/mm/yyyy": "%d/%m/%Y",
        "mm-dd-yyyy": "%m-%d-%Y",
    }
    normalized_format = _normalize_type_label(format_alias or "")
    strptime_format = format_map.get(normalized_format)

    if strptime_format:
        try:
            parsed = datetime.strptime(str(value), strptime_format)
            return parsed.date(), []
        except (ValueError, TypeError):
            return value, [
                _compose_error(
                    message,
                    "formato de fecha inválido",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_field,
                    dependent_value=dependent_value,
                ),
            ]

    try:
        timestamp = pd.to_datetime(value, errors="raise")
    except Exception:  # pragma: no cover - pandas raises multiple exceptions
        return value, [
            _compose_error(
                message,
                "fecha inválida",
                column_name=column_name,
                cell_value=value,
                dependent_field=dependent_field,
                dependent_value=dependent_value,
            ),
        ]
    return timestamp.date(), []


def _validate_dependency_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    row_context: Mapping[str, Any],
    base_parser: Callable[[Any], tuple[Any, str | None]] | None,
    column_lookup: Mapping[str, str] | None = None,
    column_tokens: Mapping[str, tuple[str, ...]] | None = None,
    rule_headers: Mapping[str, str] | None = None,
    **_: Any,
) -> tuple[Any, list[str]]:
    fallback_value, fallback_errors = _apply_base_parser(
        value, column_name, base_parser, message
    )

    specific_rules = _extract_specific_dependency_rules(rule_config)

    if not isinstance(specific_rules, list) or not specific_rules:
        return fallback_value, fallback_errors

    dependent_name: str | None = None
    normalized_dependent_name: str | None = None

    for entry in specific_rules:
        if not isinstance(entry, Mapping) or len(entry) < 2:
            continue

        dependent_candidates = [
            key
            for key, candidate in entry.items()
            if isinstance(key, str)
            and key.strip()
            and not isinstance(candidate, Mapping)
        ]

        if not dependent_candidates:
            continue

        if len(dependent_candidates) > 1:
            dependent_name = None
            normalized_dependent_name = None
            break

        candidate_name = dependent_candidates[0]
        normalized_candidate = _normalize_type_label(candidate_name)

        if dependent_name is None:
            dependent_name = candidate_name
            normalized_dependent_name = normalized_candidate
        elif normalized_dependent_name != normalized_candidate:
            dependent_name = None
            normalized_dependent_name = None
            break

    if not dependent_name or not normalized_dependent_name:
        return fallback_value, fallback_errors

    found_dependent = False
    dependent_current: Any = None
    resolved_header: str | None = None
    if rule_headers:
        mapped_header = rule_headers.get(normalized_dependent_name)
        if mapped_header and mapped_header in row_context:
            dependent_current = row_context.get(mapped_header)
            found_dependent = True
            resolved_header = mapped_header

    for key, candidate in row_context.items():
        if isinstance(key, str) and _labels_match(
            key,
            normalized_dependent_name,
            dependent_name,
        ):
            dependent_current = candidate
            found_dependent = True
            resolved_header = key
            break

    if not found_dependent and column_lookup:
        mapped = column_lookup.get(normalized_dependent_name)
        if mapped and mapped in row_context:
            dependent_current = row_context.get(mapped)
            found_dependent = True
            resolved_header = mapped

    if not found_dependent and column_tokens:
        target_tokens = _tokenize_label(dependent_name)
        token_matches = [
            header
            for header, tokens in column_tokens.items()
            if header in row_context
            and tokens
            and tokens == target_tokens
        ]
        if not token_matches and target_tokens:
            target_set = set(target_tokens)
            token_matches = [
                header
                for header, tokens in column_tokens.items()
                if header in row_context
                and tokens
                and (
                    target_set.issubset(set(tokens))
                    or set(tokens).issubset(target_set)
                )
            ]
        if len(token_matches) == 1:
            resolved_header = token_matches[0]
            dependent_current = row_context.get(resolved_header)
            found_dependent = True

    if resolved_header and not _labels_match(
        resolved_header,
        normalized_dependent_name,
        dependent_name,
    ):
        dependent_name = resolved_header
        normalized_dependent_name = _normalize_type_label(resolved_header)

    if not found_dependent:
        return fallback_value, fallback_errors

    matched = False
    accumulated_errors: list[str] = []
    resulting_value = value
    normalized_column_label = _normalize_type_label(column_name)

    def _candidate_headers(raw_key: str, normalized_key: str) -> set[str]:
        headers: set[str] = {raw_key}
        if rule_headers:
            mapped = rule_headers.get(normalized_key)
            if isinstance(mapped, str) and mapped:
                headers.add(mapped)
        if column_lookup:
            mapped = column_lookup.get(normalized_key)
            if isinstance(mapped, str) and mapped:
                headers.add(mapped)
        if column_tokens:
            tokenized_raw = _tokenize_label(raw_key)
            if tokenized_raw:
                for header, tokens in column_tokens.items():
                    if header in headers or not tokens:
                        continue
                    if tokens == tokenized_raw or set(tokens) == set(tokenized_raw):
                        headers.add(header)
        return headers

    def _targets_field(
        headers: set[str],
        normalized_expected: str,
        expected_label: str | None,
    ) -> bool:
        for header in headers:
            if _labels_match(header, normalized_expected, expected_label):
                return True
        return False

    def _config_belongs_to_current_column(
        raw_label: str, config: Mapping[str, Any]
    ) -> bool:
        """Infer whether a direct dependency config should validate this column.

        Prefer explicit header mappings first. If we cannot map the dependency
        subtype (`Documento`, `Texto`, etc.) to the current column with enough
        confidence, skip the validator instead of applying it to the wrong
        field.
        """

        matched_current = False
        matched_other = False
        normalized_raw_label = _normalize_type_label(raw_label)
        raw_headers = _candidate_headers(raw_label, normalized_raw_label)

        if _targets_field(raw_headers, normalized_column_label, column_name):
            matched_current = True
        elif any(header in row_context and header != column_name for header in raw_headers):
            matched_other = True

        for param_key in config:
            if not isinstance(param_key, str) or not param_key.strip():
                continue

            normalized_param = _normalize_type_label(param_key)
            param_headers = _candidate_headers(param_key, normalized_param)

            if _targets_field(
                param_headers,
                normalized_column_label,
                column_name,
            ):
                matched_current = True
                continue

            if any(header in row_context and header != column_name for header in param_headers):
                matched_other = True

        if matched_current:
            return True
        if matched_other:
            return False
        return False

    for entry in specific_rules:
        if not isinstance(entry, Mapping) or len(entry) < 2:
            accumulated_errors.append(
                _compose_error(
                    message,
                    "configuración dependiente inválida",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_name,
                    dependent_value=dependent_current,
                )
            )
            continue

        trigger_value: Any | None = None
        validators: list[
            tuple[
                str,
                Callable[[Any, str, Mapping[str, Any], str | None], tuple[Any, list[str]]],
                Mapping[str, Any],
            ]
        ] = []

        for raw_key, config in entry.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                accumulated_errors.append(
                    _compose_error(
                        message,
                        "clave de dependencia inválida",
                        column_name=column_name,
                        cell_value=value,
                        dependent_field=dependent_name,
                        dependent_value=dependent_current,
                    )
                )
                validators = []
                trigger_value = None
                break

            normalized_key = _normalize_type_label(raw_key)
            handler = _DEPENDENCY_RULE_HANDLERS.get(normalized_key)
            candidate_headers = _candidate_headers(raw_key, normalized_key)

            if _targets_field(
                candidate_headers,
                normalized_dependent_name,
                dependent_name,
            ):
                if isinstance(config, Mapping):
                    # Las reglas específicas solo deben evaluar la columna principal.
                    # Permite que la configuración del dependiente exista, pero no
                    # ejecutamos validaciones adicionales sobre el propio dependiente.
                    continue

                trigger_value = config
                continue

            if handler is not None and isinstance(config, Mapping):
                nested_validator_added = False
                for nested_key, nested_value in config.items():
                    if not isinstance(nested_key, str) or not nested_key.strip():
                        continue

                    nested_normalized_key = _normalize_type_label(nested_key)
                    nested_candidate_headers = _candidate_headers(
                        nested_key, nested_normalized_key
                    )
                    if not _targets_field(
                        nested_candidate_headers,
                        normalized_column_label,
                        column_name,
                    ):
                        continue

                    normalized_nested_config: Mapping[str, Any]
                    if isinstance(nested_value, Mapping):
                        normalized_nested_config = nested_value
                    elif normalized_key == "lista" and isinstance(
                        nested_value, Sequence
                    ) and not isinstance(nested_value, (str, bytes)):
                        normalized_nested_config = {"Lista": list(nested_value)}
                    else:
                        accumulated_errors.append(
                            _compose_error(
                                message,
                                f"la configuración asociada a '{nested_key}' debe ser un objeto",
                                column_name=column_name,
                                cell_value=value,
                                dependent_field=dependent_name,
                                dependent_value=dependent_current,
                            )
                        )
                        continue

                    validators.append((nested_key, handler, normalized_nested_config))
                    nested_validator_added = True

                if nested_validator_added:
                    continue

                # Cuando la regla dependiente usa un tipo de validador directo
                # (por ejemplo "Documento", "Texto", "Número") y la
                # configuración contiene sus parámetros, aplicamos ese validador
                # sobre la columna actual aunque la clave no coincida con el
                # nombre visible de la columna.
                if _config_belongs_to_current_column(raw_key, config):
                    validators.append((raw_key, handler, config))
                continue

            if not _targets_field(
                candidate_headers,
                normalized_column_label,
                column_name,
            ):
                # La configuración pertenece a otro campo distinto de la columna
                # actual, por lo que no debe evaluarse dentro de esta regla
                # dependiente.
                continue

            if handler is None:
                if (
                    normalized_key in _DEPENDENCY_METADATA_KEYS
                    or not isinstance(config, Mapping)
                ):
                    continue
                accumulated_errors.append(
                    _compose_error(
                        message,
                        f"tipo dependiente '{raw_key}' no soportado",
                        column_name=column_name,
                        cell_value=value,
                        dependent_field=dependent_name,
                        dependent_value=dependent_current,
                    )
                )
                continue

            if not isinstance(config, Mapping):
                accumulated_errors.append(
                    _compose_error(
                        message,
                        f"la configuración asociada a '{raw_key}' debe ser un objeto",
                        column_name=column_name,
                        cell_value=value,
                        dependent_field=dependent_name,
                        dependent_value=dependent_current,
                    )
                )
                continue

            validators.append((raw_key, handler, config))

        if trigger_value is None:
            accumulated_errors.append(
                _compose_error(
                    message,
                    f"falta indicar el valor para '{dependent_name}' en la configuración dependiente",
                    column_name=column_name,
                    cell_value=value,
                    dependent_field=dependent_name,
                    dependent_value=dependent_current,
                )
            )
            continue

        if not _dependency_values_equal(dependent_current, trigger_value):
            continue

        matched = True
        candidate_value = value
        candidate_errors: list[str] = []

        for raw_key, handler, config in validators:
            candidate_value, dependency_errors = handler(
                candidate_value,
                column_name,
                config,
                message,
                row_context=row_context,
                column_lookup=column_lookup,
                column_tokens=column_tokens,
                rule_headers=rule_headers,
                dependent_field=dependent_name,
                dependent_value=dependent_current,
            )
            if dependency_errors:
                candidate_errors.extend(dependency_errors)

        if candidate_errors:
            accumulated_errors.extend(candidate_errors)
            continue

        resulting_value = candidate_value

    if accumulated_errors:
        return value, accumulated_errors

    if matched:
        return resulting_value, []

    return fallback_value, fallback_errors


def _validate_joint_rule(
    *,
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    row_context: Mapping[str, Any],
    base_parser: Callable[[Any], tuple[Any, str | None]] | None,
    **_: Any,
) -> tuple[Any, list[str]]:
    fields = []
    raw_fields = rule_config.get("Nombre de campos")
    if isinstance(raw_fields, list):
        fields = [str(field) for field in raw_fields if str(field).strip()]

    if column_name not in fields:
        fields.append(column_name)

    non_empty = 0
    for field in fields:
        current = value if field == column_name else row_context.get(field)
        normalized = _normalize_cell_value(current)
        if normalized is not None:
            non_empty += 1

    if 0 < non_empty < len(fields):
        return value, [
            _compose_error(
                message,
                f"completa todos los campos relacionados ({', '.join(fields)})",
                column_name=column_name,
                cell_value=value,
            )
        ]

    parsed_value, base_errors = _apply_base_parser(
        value, column_name, base_parser, message
    )
    return parsed_value, base_errors


def _dependency_text_validator(
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    return _validate_text_rule(
        value=value,
        column_name=column_name,
        rule_config=rule_config,
        message=message,
        dependent_field=dependent_field,
        dependent_value=dependent_value,
    )


def _dependency_number_validator(
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[Any, list[str]]:
    return _validate_number_rule(
        value=value,
        column_name=column_name,
        rule_config=rule_config,
        message=message,
        dependent_field=dependent_field,
        dependent_value=dependent_value,
    )


def _dependency_document_validator(
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    return _validate_document_rule(
        value=value,
        column_name=column_name,
        rule_config=rule_config,
        message=message,
        dependent_field=dependent_field,
        dependent_value=dependent_value,
    )


def _dependency_list_validator(
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    return _validate_list_rule(
        value=value,
        column_name=column_name,
        rule_config=rule_config,
        message=message,
        dependent_field=dependent_field,
        dependent_value=dependent_value,
    )


def _dependency_full_list_validator(
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    *,
    row_context: Mapping[str, Any] | None = None,
    column_lookup: Mapping[str, str] | None = None,
    column_tokens: Mapping[str, tuple[str, ...]] | None = None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    return _validate_full_list_rule(
        value=value,
        column_name=column_name,
        rule_config=rule_config,
        message=message,
        row_context=row_context,
        column_lookup=column_lookup,
        column_tokens=column_tokens,
        dependent_field=dependent_field,
        dependent_value=dependent_value,
    )


def _dependency_phone_validator(
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    return _validate_phone_rule(
        value=value,
        column_name=column_name,
        rule_config=rule_config,
        message=message,
        dependent_field=dependent_field,
        dependent_value=dependent_value,
    )


def _dependency_email_validator(
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[str, list[str]]:
    return _validate_email_rule(
        value=value,
        column_name=column_name,
        rule_config=rule_config,
        message=message,
        dependent_field=dependent_field,
        dependent_value=dependent_value,
    )


def _dependency_date_validator(
    value: Any,
    column_name: str,
    rule_config: Mapping[str, Any],
    message: str | None,
    dependent_field: str | None = None,
    dependent_value: Any | None = None,
    **_: Any,
) -> tuple[date, list[str]]:
    return _validate_date_rule(
        value=value,
        column_name=column_name,
        rule_config=rule_config,
        message=message,
        dependent_field=dependent_field,
        dependent_value=dependent_value,
    )


def _dependency_values_equal(actual: Any, expected: Any) -> bool:
    normalized_actual = _normalize_cell_value(actual)
    normalized_expected = _normalize_cell_value(expected)

    if isinstance(normalized_actual, bool) or isinstance(normalized_expected, bool):
        return bool(normalized_actual) is bool(normalized_expected)

    numeric_types = (int, float, Decimal)
    if isinstance(normalized_actual, numeric_types) and isinstance(normalized_expected, numeric_types):
        try:
            return Decimal(str(normalized_actual)) == Decimal(str(normalized_expected))
        except (InvalidOperation, ValueError):
            return False

    if normalized_actual is None or normalized_expected is None:
        return normalized_actual is None and normalized_expected is None

    return _normalize_type_label(str(normalized_actual)) == _normalize_type_label(
        str(normalized_expected)
    )


def _normalize_choice_value(value: Any) -> str | None:
    """Normalize ``value`` for case-insensitive text comparisons."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    return normalized.casefold()


def _extract_allowed_values(rule_config: Mapping[str, Any]) -> list[Any]:
    candidate_keys = [
        "Valores permitidos",
        "Valores",
        "Lista",
        "Opciones",
        "options",
        "choices",
    ]
    for key in candidate_keys:
        values = rule_config.get(key)
        if isinstance(values, list):
            return values
    return []


def _build_grouped_rule_header_lookup(
    columns: Sequence[TemplateColumn],
    rules: Mapping[int, dict[str, Any] | list[Any]],
) -> dict[tuple[str, int], dict[str, str]]:
    grouped_lookup: dict[tuple[str, int], dict[str, str]] = {}
    occurrences_by_rule: dict[int, list[tuple[str, int | None, tuple[str, ...]]]] = {}
    expected_headers_by_rule: dict[int, set[str]] = {}

    for column in columns:
        for assignment in column.rules:
            if assignment.id not in rules or not assignment.headers:
                continue

            rule_type = _extract_validation_rule_type(rules[assignment.id])
            if rule_type not in _GROUPED_HEADER_RULE_TYPES:
                continue

            normalized_headers = tuple(
                normalized_header
                for header in assignment.headers
                for normalized_header in [_normalize_type_label(header)]
                if normalized_header
            )
            if not normalized_headers:
                continue

            occurrences_by_rule.setdefault(assignment.id, []).append(
                (column.name, assignment.group, normalized_headers)
            )
            expected_headers_by_rule.setdefault(assignment.id, set()).update(
                normalized_headers
            )

    for rule_id, occurrences in occurrences_by_rule.items():
        grouped_occurrences: dict[int, list[tuple[str, tuple[str, ...]]]] = {}
        all_grouped = all(group is not None and group > 0 for _, group, _ in occurrences)

        if all_grouped:
            for column_name, group, normalized_headers in occurrences:
                grouped_occurrences.setdefault(int(group), []).append(
                    (column_name, normalized_headers)
                )
        else:
            expected_headers = expected_headers_by_rule.get(rule_id, set())
            current_group = 1
            pending_entries: list[tuple[str, tuple[str, ...]]] = []
            covered_headers: set[str] = set()

            for column_name, _group, normalized_headers in occurrences:
                pending_entries.append((column_name, normalized_headers))
                covered_headers.update(normalized_headers)

                if expected_headers and expected_headers.issubset(covered_headers):
                    grouped_occurrences[current_group] = list(pending_entries)
                    current_group += 1
                    pending_entries = []
                    covered_headers = set()

            if pending_entries:
                grouped_occurrences[current_group] = list(pending_entries)

        for entries in grouped_occurrences.values():
            header_map: dict[str, str] = {}
            for column_name, normalized_headers in entries:
                for normalized_header in normalized_headers:
                    header_map.setdefault(normalized_header, column_name)

            for column_name, _normalized_headers in entries:
                grouped_lookup[(column_name, rule_id)] = dict(header_map)

    return grouped_lookup


def _extract_validation_rule_type(rule_definition: dict[str, Any] | list[Any]) -> str:
    if isinstance(rule_definition, Mapping):
        definitions = [rule_definition]
    elif isinstance(rule_definition, list):
        definitions = [entry for entry in rule_definition if isinstance(entry, Mapping)]
    else:
        definitions = []

    for definition in definitions:
        candidate = definition.get("Tipo de dato")
        if isinstance(candidate, str):
            normalized_candidate = _normalize_type_label(candidate)
            if normalized_candidate:
                return normalized_candidate
    return ""


def _extract_specific_dependency_rules(
    rule_config: Mapping[str, Any]
) -> list[Mapping[str, Any]] | None:
    for key, value in rule_config.items():
        if isinstance(key, str) and _normalize_type_label(key) == "reglas especifica":
            if isinstance(value, list):
                return value
            return None

    fallback = rule_config.get("reglas especifica")
    if isinstance(fallback, list):
        return fallback

    # Algunas reglas generadas desde herramientas externas anidan la definición
    # específica dentro de otro bloque "Regla". Permitimos esa estructura para
    # mantener compatibilidad con reglas existentes.
    nested_candidates = [
        nested
        for key, nested in rule_config.items()
        if isinstance(key, str)
        and _normalize_type_label(key) == "regla"
        and isinstance(nested, Mapping)
    ]
    for nested in nested_candidates:
        extracted = _extract_specific_dependency_rules(nested)
        if extracted:
            return extracted

    return None


def _extract_composite_combinations(rule_config: Mapping[str, Any]) -> list[dict[str, str]]:
    candidate_keys = [
        "Lista compleja",
        "Lista",
        "Listas",
        "Combinaciones",
    ]
    for key in candidate_keys:
        raw_values = rule_config.get(key)
        if not isinstance(raw_values, list):
            continue
        combinations: list[dict[str, str]] = []
        for entry in raw_values:
            if not isinstance(entry, Mapping):
                continue
            normalized_entry: dict[str, str] = {}
            for campo, valor in entry.items():
                if not isinstance(campo, str):
                    continue
                field_name = campo.strip()
                if not field_name:
                    continue
                normalized_value = _normalize_cell_value(valor)
                if normalized_value is None:
                    continue
                if isinstance(normalized_value, (Mapping, Sequence)) and not isinstance(
                    normalized_value, (str, bytes)
                ):
                    continue
                normalized_entry[field_name] = str(normalized_value)
            if normalized_entry:
                combinations.append(normalized_entry)
        if combinations:
            return combinations
    return []


def _coerce_timestamp(value: Any) -> Any:
    pd = _get_pandas_module()
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is not None:
            value = value.tz_convert(None)
        return value.to_pydatetime()
    return value


def _stringify_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, "f").rstrip("0").rstrip(".")
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f").rstrip("0").rstrip(".")
    return str(value)


def _parse_string(value: Any) -> tuple[Any, str | None]:
    if value is None:
        return None, None
    return _stringify_scalar(value), None


def _parse_text(value: Any) -> tuple[Any, str | None]:
    return _parse_string(value)


def _parse_integer(value: Any) -> tuple[Any, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "debe ser un número entero"
    if isinstance(value, int):
        return value, None
    if isinstance(value, float) and value.is_integer():
        return int(value), None
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation:
        return None, "debe ser un número entero"
    if decimal_value != decimal_value.to_integral_value():
        return None, "debe ser un número entero"
    return int(decimal_value), None


def _parse_float(value: Any) -> tuple[Any, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "debe ser un número"
    try:
        return float(value), None
    except (TypeError, ValueError):
        try:
            return float(Decimal(str(value))), None
        except (InvalidOperation, ValueError):
            return None, "debe ser un número"


_TRUTHY = {"true", "1", "yes", "si", "sí"}
_FALSY = {"false", "0", "no"}


def _parse_boolean(value: Any) -> tuple[Any, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return value, None
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value), None
        return None, "debe ser un valor booleano"
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True, None
    if text in _FALSY:
        return False, None
    return None, "debe ser un valor booleano"


def _parse_date(value: Any) -> tuple[Any, str | None]:
    pd = _get_pandas_module()
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value.date(), None
    try:
        timestamp = pd.to_datetime(value, errors="raise")
    except Exception:  # pragma: no cover - pandas raises multiple exceptions
        return None, "debe ser una fecha válida"
    return timestamp.date(), None


def _parse_datetime(value: Any) -> tuple[Any, str | None]:
    pd = _get_pandas_module()
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value, None
    try:
        timestamp = pd.to_datetime(value, errors="raise")
    except Exception:  # pragma: no cover - pandas raises multiple exceptions
        return None, "debe ser una fecha y hora válida"
    return timestamp.to_pydatetime(), None


def _parse_json(value: Any) -> tuple[Any, str | None]:
    if value is None:
        return None, None
    if isinstance(value, (dict, list)):
        return value, None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return None, "debe contener un JSON válido"
    return parsed, None


_TypeParser = Callable[[Any], tuple[Any, str | None]]

_TYPE_PARSERS: dict[str, _TypeParser] = {
    "string": _parse_string,
    "text": _parse_text,
    "integer": _parse_integer,
    "float": _parse_float,
    "boolean": _parse_boolean,
    "date": _parse_date,
    "datetime": _parse_datetime,
    "json": _parse_json,
    "texto": _parse_text,
    "numero": _parse_float,
    "documento": _parse_string,
    "lista": _parse_string,
    "lista compleja": _parse_string,
    "telefono": _parse_string,
    "correo": _parse_string,
    "fecha": _parse_date,
    "dependencia": _parse_string,
    "validacion conjunta": _parse_string,
}


RuleValidator = Callable[..., tuple[Any, list[str]]]

_RULE_VALIDATORS: dict[str, RuleValidator] = {
    "texto": _validate_text_rule,
    "documento": _validate_document_rule,
    "numero": _validate_number_rule,
    "lista": _validate_list_rule,
    "lista compleja": _validate_full_list_rule,
    "telefono": _validate_phone_rule,
    "correo": _validate_email_rule,
    "fecha": _validate_date_rule,
    "dependencia": _validate_dependency_rule,
    "validacion conjunta": _validate_joint_rule,
}

_DEPENDENCY_METADATA_KEYS: set[str] = {
    "ejemplo",
    "ejemplos",
    "example",
    "examples",
    "descripcion",
    "descripcion general",
    "descripcion corta",
    "notas",
    "nota",
    "notes",
}

_DEPENDENCY_RULE_HANDLERS: dict[
    str, Callable[[Any, str, Mapping[str, Any], str | None], tuple[Any, list[str]]]
] = {
    "texto": _dependency_text_validator,
    "numero": _dependency_number_validator,
    "documento": _dependency_document_validator,
    "lista": _dependency_list_validator,
    "lista compleja": _dependency_full_list_validator,
    "telefono": _dependency_phone_validator,
    "correo": _dependency_email_validator,
    "fecha": _dependency_date_validator,
}


__all__ = [
    "get_load",
    "get_load_report",
    "get_load_original_file",
    "list_loads",
    "upload_template_load",
    "process_template_load",
]
