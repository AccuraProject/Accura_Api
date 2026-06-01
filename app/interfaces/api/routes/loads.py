"""Rutas de la API relacionadas con cargas de datos para plantillas."""

import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pathlib import Path
from sqlalchemy.orm import Session

from app.application.use_cases.loads import (
    get_load as get_load_uc,
    get_load_original_file as get_load_original_file_uc,
    get_load_report as get_load_report_uc,
    list_loads as list_loads_uc,
    list_loads_with_templates as list_loads_with_templates_uc,
    process_template_load as process_template_load_uc,
    upload_template_load as upload_template_load_uc,
)
from app.domain.entities import Load, Template, User
from app.infrastructure.database import SessionLocal, get_db
from app.interfaces.api.dependencies import get_current_active_user
from app.interfaces.api.schemas import (
    LoadRead,
    LoadUploadResponse,
    LoadWithTemplateSummaryRead,
    TemplateSummaryRead,
    UserSummaryRead,
)

router = APIRouter(tags=["loads"])
logger = logging.getLogger(__name__)


def _load_to_read_model(load: Load) -> LoadRead:
    if hasattr(LoadRead, "model_validate"):
        return LoadRead.model_validate(load)
    return LoadRead.from_orm(load)


def _template_summary_to_read_model(template: Template) -> TemplateSummaryRead:
    if hasattr(TemplateSummaryRead, "model_validate"):
        return TemplateSummaryRead.model_validate(template)
    return TemplateSummaryRead.from_orm(template)


def _user_summary_to_read_model(user: User) -> UserSummaryRead:
    if hasattr(UserSummaryRead, "model_validate"):
        return UserSummaryRead.model_validate(user)
    return UserSummaryRead.from_orm(user)


def _schedule_cleanup(background_tasks: BackgroundTasks, path: Path) -> None:
    background_tasks.add_task(_remove_file_safely, path)


def _remove_file_safely(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:  # pragma: no cover - best effort cleanup
        pass


@router.post(
    "/templates/{template_id}/loads",
    response_model=LoadUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_template_load(
    template_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> LoadUploadResponse:
    """Sube información para la plantilla indicada y agenda su validación en segundo plano."""

    try:
        file_bytes = file.file.read()
    finally:
        file.file.seek(0)

    try:
        load = upload_template_load_uc(
            db,
            template_id=template_id,
            user=current_user,
            file_bytes=file_bytes,
            filename=file.filename or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if load.id is None:  # pragma: no cover - defensive guard
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No fue posible registrar la carga",
        )

    background_tasks.add_task(
        _process_load_in_background,
        load_id=load.id,
        template_id=template_id,
        user_id=current_user.id,
        file_bytes=file_bytes,
        filename=file.filename or "",
    )

    return LoadUploadResponse(
        message="Archivo cargado correctamente",
        load=_load_to_read_model(load),
    )


def _process_load_in_background(
    *,
    load_id: int,
    template_id: int,
    user_id: int,
    file_bytes: bytes,
    filename: str,
) -> None:
    """Procesa la carga utilizando una sesión de base de datos independiente."""

    session = SessionLocal()
    try:
        process_template_load_uc(
            session,
            load_id=load_id,
            template_id=template_id,
            user_id=user_id,
            file_bytes=file_bytes,
            filename=filename,
        )
    except Exception as exc:  # pragma: no cover - background processing guard
        logger.exception("Error al procesar la carga %s: %s", load_id, exc)
    finally:
        session.close()


@router.get("/loads", response_model=list[LoadRead])
def list_loads(
    template_id: int | None = Query(default=None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[LoadRead]:
    """Devuelve las cargas visibles para el usuario autenticado."""

    loads = list_loads_uc(
        db,
        current_user=current_user,
        template_id=template_id,
        skip=skip,
        limit=limit,
    )
    return [_load_to_read_model(load) for load in loads]


@router.get("/loads/details", response_model=list[LoadWithTemplateSummaryRead])
def list_loads_with_template_details(
    template_id: int | None = Query(default=None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[LoadWithTemplateSummaryRead]:
    """Devuelve el historial de cargas junto con información resumida de su plantilla."""

    loads_with_templates = list_loads_with_templates_uc(
        db,
        current_user=current_user,
        template_id=template_id,
        skip=skip,
        limit=limit,
    )

    return [
        LoadWithTemplateSummaryRead(
            load=_load_to_read_model(load),
            template=_template_summary_to_read_model(template),
            user=_user_summary_to_read_model(user),
        )
        for load, template, user in loads_with_templates
    ]


@router.get("/loads/{load_id}", response_model=LoadRead)
def read_load(
    load_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> LoadRead:
    """Obtiene la carga identificada por ``load_id`` si el usuario tiene acceso."""

    try:
        load = get_load_uc(db, load_id=load_id, current_user=current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _load_to_read_model(load)


@router.get("/loads/{load_id}/report")
def download_load_report(
    load_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> FileResponse:
    """Descarga el reporte en Excel generado para la carga solicitada."""

    try:
        load, path, filename = get_load_report_uc(
            db, load_id=load_id, current_user=current_user
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    _schedule_cleanup(background_tasks, path)
    return FileResponse(
        path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=background_tasks,
    )


@router.get("/loads/{load_id}/source")
def download_load_source_file(
    load_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> FileResponse:
    """Descarga el archivo original cargado por el usuario sin columnas adicionales."""

    try:
        load, path, filename = get_load_original_file_uc(
            db, load_id=load_id, current_user=current_user
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    suffix = path.suffix.lower()
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".csv":
        media_type = "text/csv"

    _schedule_cleanup(background_tasks, path)
    return FileResponse(
        path,
        filename=filename,
        media_type=media_type,
        background=background_tasks,
    )


__all__ = ["router"]
