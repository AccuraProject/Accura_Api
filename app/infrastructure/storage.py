"""Azure Blob Storage utilities for file management."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

from app.config import get_settings


@lru_cache
def _get_blob_service_client() -> BlobServiceClient:
    settings = get_settings()
    if not settings.azure_storage_connection_string:
        msg = "Azure storage connection string is not configured"
        raise RuntimeError(msg)
    return BlobServiceClient.from_connection_string(
        settings.azure_storage_connection_string
    )


@lru_cache
def _get_container_name() -> str:
    settings = get_settings()
    if not settings.azure_storage_container_name:
        msg = "Azure storage container name is not configured"
        raise RuntimeError(msg)
    return settings.azure_storage_container_name


@lru_cache
def _get_container_client():
    service_client = _get_blob_service_client()
    container_name = _get_container_name()
    try:
        service_client.create_container(container_name)
    except ResourceExistsError:
        pass
    return service_client.get_container_client(container_name)


def upload_blob(
    blob_path: str,
    data: bytes,
    *,
    content_type: Optional[str] = None,
) -> None:
    """Upload ``data`` to the configured storage container at ``blob_path``."""

    container_client = _get_container_client()
    blob_client = container_client.get_blob_client(blob_path)
    content_settings = None
    if content_type is not None:
        content_settings = ContentSettings(content_type=content_type)
    blob_client.upload_blob(
        data,
        overwrite=True,
        content_settings=content_settings,
    )


def delete_blob(blob_path: str) -> None:
    """Delete the blob located at ``blob_path`` if it exists."""

    container_client = _get_container_client()
    blob_client = container_client.get_blob_client(blob_path)
    try:
        blob_client.delete_blob()
    except ResourceNotFoundError:
        return


def download_blob_to_path(blob_path: str, destination: Path) -> Path:
    """Download the blob located at ``blob_path`` into ``destination``."""

    container_client = _get_container_client()
    blob_client = container_client.get_blob_client(blob_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = blob_client.download_blob()
    except ResourceNotFoundError as exc:  # pragma: no cover - network edge case
        raise FileNotFoundError(blob_path) from exc
    data = stream.readall()
    destination.write_bytes(data)
    return destination


def build_downloadable_blob_url(
    blob_path: str,
    *,
    download_name: str | None = None,
    expires_in_hours: int = 24 * 30,
) -> str:
    """Return a time-limited downloadable URL for ``blob_path``."""

    if not blob_path:
        msg = "Blob path is required"
        raise ValueError(msg)

    service_client = _get_blob_service_client()
    account_name = service_client.account_name
    credential = getattr(service_client.credential, "account_key", None)
    if not account_name or not credential:
        msg = "Azure storage account key is required to generate blob download URLs"
        raise RuntimeError(msg)

    from datetime import datetime, timedelta, timezone

    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=_get_container_name(),
        blob_name=blob_path,
        account_key=credential,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=expires_in_hours),
        content_disposition=(
            f'attachment; filename="{download_name}"' if download_name else "attachment"
        ),
    )
    blob_client = _get_container_client().get_blob_client(blob_path)
    return f"{blob_client.url}?{sas_token}"


def extract_blob_path_from_url(blob_url: str) -> str | None:
    """Extract the blob path relative to the container from a blob URL."""

    if not blob_url:
        return None

    parsed = urlparse(blob_url)
    path = parsed.path.lstrip("/")
    container_name = _get_container_name().strip("/")
    prefix = f"{container_name}/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return None


__all__ = [
    "build_downloadable_blob_url",
    "upload_blob",
    "delete_blob",
    "download_blob_to_path",
    "extract_blob_path_from_url",
]
