"""Rutas para administrar plantillas, sus columnas y accesos."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.application.use_cases.template_columns import (
    NewTemplateColumnData,
    NewTemplateColumnRuleData,
    replace_template_columns,
    create_template_column as create_template_column_uc,
    create_template_columns as create_template_columns_uc,
    delete_template_column as delete_template_column_uc,
    get_template_column as get_template_column_uc,
    list_template_columns as list_template_columns_uc,
    replace_template_columns as replace_template_columns_uc,
)
from app.application.use_cases.templates import (
    bulk_grant_template_access as bulk_grant_template_access_uc,
    bulk_revoke_template_access as bulk_revoke_template_access_uc,
    bulk_update_template_access as bulk_update_template_access_uc,
    create_template as create_template_uc,
    duplicate_template as duplicate_template_uc,
    delete_template as delete_template_uc,
    get_template as get_template_uc,
    get_template_detail as get_template_detail_uc,
    get_template_excel as get_template_excel_uc,
    list_template_access as list_template_access_uc,
    list_templates as list_templates_uc,
    list_user_template_access as list_user_template_access_uc,
    list_user_templates as list_user_templates_uc,
    update_template as update_template_uc,
    update_template_status as update_template_status_uc,
)
from app.domain.entities import Template, TemplateColumn, TemplateUserAccess, User
from app.infrastructure.database import get_db
from app.infrastructure.repositories import TemplateRepository
from app.interfaces.api.dependencies import (
    get_current_active_user,
    require_admin,
)
from app.interfaces.api.schemas import (
    TemplateColumnBulkCreate,
    TemplateColumnBulkUpdate,
    TemplateColumnCreate,
    TemplateColumnRead,
    TemplateColumnRule as TemplateColumnRuleSchema,
    TemplateColumnUpdate,
    TemplateCreate,
    TemplateRead,
    TemplateStatusUpdate,
    TemplateUpdate,
    TemplateUserAccessGrantList,
    TemplateUserAccessRead,
    TemplateUserAccessRevokeList,
    TemplateUserAccessUpdateList,
)

router = APIRouter(prefix="/templates", tags=["templates"])


def _template_to_read_model(template: Template) -> TemplateRead:
    if hasattr(TemplateRead, "model_validate"):
        return TemplateRead.model_validate(template)
    return TemplateRead.from_orm(template)


def _remove_file_safely(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:  # pragma: no cover - best-effort cleanup
        pass


def _schedule_cleanup(background_tasks: BackgroundTasks, path: Path) -> None:
    background_tasks.add_task(_remove_file_safely, path)


def _column_to_read_model(
    column: TemplateColumn,
    *,
    rule_definitions: Mapping[int, Any] | None = None,
) -> TemplateColumnRead:
    rules_payload: list[dict[str, object]] = []
    for rule in column.rules:
        entry: dict[str, object] = {"id": rule.id}
        if rule.headers:
            entry["header rule"] = list(rule.headers)
        if rule_definitions and rule.id in rule_definitions:
            entry["rule"] = rule_definitions[rule.id]
        rules_payload.append(entry)

    payload = {
        "id": column.id,
        "template_id": column.template_id,
        "name": column.name,
        "data_type": column.data_type,
        "description": column.description,
        "rules": rules_payload,
        "created_at": column.created_at,
        "updated_at": column.updated_at,
        "is_active": column.is_active,
        "deleted": column.deleted,
        "deleted_by": column.deleted_by,
        "deleted_at": column.deleted_at,
    }

    if hasattr(TemplateColumnRead, "model_validate"):
        return TemplateColumnRead.model_validate(payload)
    return TemplateColumnRead(**payload)


def _template_detail_to_read_model(
    template: Template, rule_definitions: Mapping[int, Any]
) -> TemplateRead:
    columns = [
        _column_to_read_model(column, rule_definitions=rule_definitions)
        for column in template.columns
    ]
    columns_payload = [_dump_model(column) for column in columns]

    payload = {
        "id": template.id,
        "user_id": template.user_id,
        "name": template.name,
        "status": template.status,
        "description": template.description,
        "table_name": template.table_name,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
        "is_active": template.is_active,
        "deleted": template.deleted,
        "deleted_by": template.deleted_by,
        "deleted_at": template.deleted_at,
        "columns": columns_payload,
    }

    if hasattr(TemplateRead, "model_validate"):
        return TemplateRead.model_validate(payload)
    return TemplateRead(**payload)


def _map_rule_payload(
    rules: list[TemplateColumnRuleSchema] | None,
) -> list[NewTemplateColumnRuleData] | None:
    if not rules:
        return None

    mapped: list[NewTemplateColumnRuleData] = []
    for rule in rules:
        mapped.append(
            NewTemplateColumnRuleData(
                id=rule.id,
                header_rule=rule.header_rule,
            )
        )
    return mapped


def _access_to_read_model(access: TemplateUserAccess) -> TemplateUserAccessRead:
    """Convert a ``TemplateUserAccess`` entity into the API read model."""

    if hasattr(TemplateUserAccessRead, "model_validate"):
        if isinstance(access, dict):  # pragma: no cover - compatibility path
            return TemplateUserAccessRead.model_validate(access)
        return TemplateUserAccessRead.model_validate(access, from_attributes=True)
    if isinstance(access, dict):  # pragma: no cover - compatibility for pydantic v1
        return TemplateUserAccessRead(**access)
    return TemplateUserAccessRead.from_orm(access)  # pragma: no cover


def _dump_model(model: object) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    raise TypeError("Unsupported model type for serialization")


@router.post("/", response_model=TemplateRead, status_code=status.HTTP_201_CREATED)
def register_template(
    template_in: TemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> TemplateRead:
    """Crea una nueva definición de plantilla."""

    try:
        template = create_template_uc(
            db,
            user_id=current_user.id,
            name=template_in.name,
            table_name=template_in.table_name,
            description=template_in.description,
            created_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return _template_to_read_model(template)


@router.post(
    "/{template_id}/duplicate",
    response_model=TemplateRead,
    status_code=status.HTTP_201_CREATED,
)
def duplicate_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> TemplateRead:
    """Duplica una plantilla existente usando solo el ``template_id``."""

    try:
        template = duplicate_template_uc(
            db,
            template_id=template_id,
            created_by=current_user.id,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail == "Plantilla no encontrada":
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return _template_to_read_model(template)


@router.get("/", response_model=list[TemplateRead])
def list_templates(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> list[TemplateRead]:
    """Devuelve una lista paginada de plantillas registradas."""

    templates = list_templates_uc(
        db, current_user=current_user, skip=skip, limit=limit
    )
    return [_template_to_read_model(template) for template in templates]


@router.get("/users/{user_id}", response_model=list[TemplateRead])
def list_templates_for_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[TemplateRead]:
    """Devuelve las plantillas publicadas a las que el usuario tiene acceso."""

    try:
        templates = list_user_templates_uc(
            db, user_id=user_id, current_user=current_user
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "Usuario no encontrado":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=detail
            ) from exc
        if detail == "No autorizado":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=detail
            ) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    return [_template_to_read_model(template) for template in templates]


@router.get("/users/{user_id}/access", response_model=list[TemplateUserAccessRead])
def list_template_accesses_for_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[TemplateUserAccessRead]:
    """Devuelve los accesos a plantillas configurados para el usuario indicado."""

    try:
        accesses = list_user_template_access_uc(
            db, user_id=user_id, current_user=current_user
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "Usuario no encontrado":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=detail
            ) from exc
        if detail == "No autorizado":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=detail
            ) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    return [_access_to_read_model(access) for access in accesses]


@router.post(
    "/access",
    response_model=list[TemplateUserAccessRead],
    status_code=status.HTTP_201_CREATED,
)
def grant_template_accesses(
    payload: TemplateUserAccessGrantList,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[TemplateUserAccessRead]:
    """Concede acceso a una o varias plantillas para los usuarios indicados."""

    try:
        accesses = bulk_grant_template_access_uc(
            db,
            grants=[_dump_model(item) for item in payload],
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail in {"Plantilla no encontrada", "Usuario no encontrado"}:
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return [_access_to_read_model(access) for access in accesses]


@router.get(
    "/access",
    response_model=list[TemplateUserAccessRead],
)
def list_template_access(
    template_id: int = Query(..., ge=1),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> list[TemplateUserAccessRead]:
    """Lista los accesos configurados para la plantilla solicitada."""

    try:
        accesses = list_template_access_uc(
            db,
            template_id=template_id,
            include_inactive=include_inactive,
            current_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return [_access_to_read_model(access) for access in accesses]


@router.put(
    "/access",
    response_model=list[TemplateUserAccessRead],
)
def update_template_accesses(
    payload: TemplateUserAccessUpdateList,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[TemplateUserAccessRead]:
    """Actualiza la ventana de acceso configurada para uno o varios accesos."""

    try:
        accesses = bulk_update_template_access_uc(
            db,
            updates=[_dump_model(item) for item in payload],
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail in {"Plantilla no encontrada", "Acceso no encontrado"}:
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return [_access_to_read_model(access) for access in accesses]


@router.post(
    "/access/revoke",
    response_model=list[TemplateUserAccessRead],
)
def revoke_template_accesses(
    payload: TemplateUserAccessRevokeList,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> list[TemplateUserAccessRead]:
    """Revoca uno o varios accesos previamente concedidos."""

    try:
        accesses = bulk_revoke_template_access_uc(
            db,
            revocations=[_dump_model(item) for item in payload],
            revoked_by=current_user.id,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail in {"Plantilla no encontrada", "Acceso no encontrado"}:
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return [_access_to_read_model(access) for access in accesses]


@router.get("/{template_id}", response_model=TemplateRead)
def read_template(
    template_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> TemplateRead:
    """Obtiene la plantilla identificada por ``template_id``."""

    try:
        template = get_template_uc(db, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _template_to_read_model(template)


@router.get("/{template_id}/detail", response_model=TemplateRead)
def read_template_detail(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> TemplateRead:
    """Obtiene los detalles completos de la plantilla para el usuario actual."""

    try:
        template = get_template_detail_uc(
            db, template_id=template_id, requesting_user=current_user
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "Plantilla no encontrada":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from exc
        if detail == "El usuario no tiene acceso a la plantilla":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    repository = TemplateRepository(db)
    rule_definitions = repository.get_rule_payloads(template.id)
    return _template_detail_to_read_model(template, rule_definitions)


@router.put("/{template_id}", response_model=TemplateRead)
def update_template(
    template_id: int,
    template_in: TemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> TemplateRead:
    """Actualiza una plantilla existente."""

    if hasattr(template_in, "model_dump"):
        update_data = template_in.model_dump(exclude_unset=True)
    else:  # pragma: no cover - compatibility path for pydantic v1
        update_data = template_in.dict(exclude_unset=True)

    try:
        template = update_template_uc(
            db,
            template_id=template_id,
            name=update_data.get("name"),
            description=update_data.get("description"),
            status=update_data.get("status"),
            table_name=update_data.get("table_name"),
            is_active=update_data.get("is_active"),
            updated_by=current_user.id,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail in {"Plantilla no encontrada"}:
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return _template_to_read_model(template)


@router.patch("/{template_id}/status", response_model=TemplateRead)
def update_template_status(
    template_id: int,
    status_in: TemplateStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> TemplateRead:
    """Actualiza únicamente el estado de una plantilla."""

    try:
        template = update_template_status_uc(
            db,
            template_id=template_id,
            status=status_in.status,
            updated_by=current_user.id,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail in {"Plantilla no encontrada"}:
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return _template_to_read_model(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    """Elimina una plantilla junto con su tabla dinámica."""

    try:
        delete_template_uc(db, template_id, deleted_by=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{template_id}/columns",
    response_model=TemplateColumnRead | list[TemplateColumnRead],
    status_code=status.HTTP_201_CREATED,
)
def register_template_column(
    template_id: int,
    column_in: TemplateColumnCreate
    | list[TemplateColumnCreate]
    | TemplateColumnBulkCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> TemplateColumnRead | list[TemplateColumnRead]:
    """Crea una o varias columnas asociadas a la plantilla."""

    try:
        if isinstance(column_in, TemplateColumnCreate):
            column = create_template_column_uc(
                db,
                template_id=template_id,
                name=column_in.name,
                description=column_in.description,
                rules=_map_rule_payload(column_in.rules),
                created_by=current_user.id,
            )
            result = _column_to_read_model(column)
        else:
            if isinstance(column_in, TemplateColumnBulkCreate):
                incoming_columns = column_in.columns
            else:
                incoming_columns = column_in
            payload = [
                NewTemplateColumnData(
                    name=col.name,
                    description=col.description,
                    rules=_map_rule_payload(col.rules),
                )
                for col in incoming_columns
            ]
            columns = create_template_columns_uc(
                db,
                template_id=template_id,
                columns=payload,
                created_by=current_user.id,
            )
            result = [_column_to_read_model(column) for column in columns]
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail in {"Plantilla no encontrada", "Columna no encontrada"}:
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return result


@router.get("/{template_id}/columns", response_model=list[TemplateColumnRead])
def list_template_columns(
    template_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[TemplateColumnRead]:
    """Lista todas las columnas configuradas para la plantilla."""

    try:
        columns = list_template_columns_uc(db, template_id=template_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return [_column_to_read_model(column) for column in columns]


@router.get(
    "/{template_id}/columns/{column_id}", response_model=TemplateColumnRead
)
def read_template_column(
    template_id: int,
    column_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> TemplateColumnRead:
    """Obtiene la información de una columna específica."""

    try:
        column = get_template_column_uc(db, template_id=template_id, column_id=column_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _column_to_read_model(column)


@router.put(
    "/{template_id}/columns",
    response_model=TemplateColumnRead | list[TemplateColumnRead],
)
def update_template_columns(
    template_id: int,
    column_in: TemplateColumnUpdate
    | list[TemplateColumnUpdate]
    | TemplateColumnBulkUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> TemplateColumnRead | list[TemplateColumnRead]:
    """Reemplaza las columnas existentes por las enviadas en la solicitud."""

    if isinstance(column_in, TemplateColumnUpdate):
        payloads = [column_in]
        single_result = True
    else:
        if isinstance(column_in, TemplateColumnBulkUpdate):
            incoming_columns = column_in.columns
        else:
            incoming_columns = column_in
        payloads = list(incoming_columns)
        single_result = False

    try:
        replacements: list[NewTemplateColumnData] = []
        for payload in payloads:
            name = payload.name
            if name is None:
                raise ValueError("El nombre de la columna es obligatorio")

            replacements.append(
                NewTemplateColumnData(
                    name=name,
                    description=payload.description,
                    rules=_map_rule_payload(payload.rules),
                    is_active=payload.is_active if payload.is_active is not None else True,
                )
            )

        updated_columns = replace_template_columns_uc(
            db,
            template_id=template_id,
            columns=replacements,
            actor_id=current_user.id,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST
        if detail in {"Plantilla no encontrada"}:
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail) from exc

    if single_result:
        return _column_to_read_model(updated_columns[0])
    return [_column_to_read_model(column) for column in updated_columns]


@router.delete(
    "/{template_id}/columns/{column_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_template_column(
    template_id: int,
    column_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    """Elimina una columna de la plantilla."""

    try:
        delete_template_column_uc(
            db,
            template_id=template_id,
            column_id=column_id,
            deleted_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{template_id}/excel")
def download_template_excel(
    template_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Descarga el archivo de Excel generado para la plantilla."""

    try:
        path, filename = get_template_excel_uc(
            db,
            template_id=template_id,
            requesting_user=current_user,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_404_NOT_FOUND
        if detail == "El usuario no tiene acceso a la plantilla":
            status_code = status.HTTP_403_FORBIDDEN
        elif detail not in {
            "Plantilla no encontrada",
            "Archivo de plantilla no encontrado",
        }:
            status_code = status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=detail) from exc

    _schedule_cleanup(background_tasks, path)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
        background=background_tasks,
    )
