"""File storage behind a small interface (ticket attachments now, receipts later).

A file is addressed by its practice and a random *storage key* (``new_key``) that never
derives from the file's name, so nothing a client sends can reach the file system. Every call
names the practice: ``LocalFileStorage`` keeps each practice's files in a directory of their
own, so a key can only be read, replaced or removed under the practice that stored it, whatever
id or key a caller passes. The files live under one directory (``STORAGE_DIR``, see
``Settings.storage_path``). ``AzureBlobFileStorage`` keeps the same layout in an Azure Blob
Storage container (``AZURE_STORAGE_CONTAINER``); ``get_storage`` picks it when one is configured.
"""

import asyncio
import os
import re
import secrets
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from azure.core.exceptions import ResourceNotFoundError

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

CHUNK_SIZE = 64 * 1024
_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# Bytes per ranged GET from Blob Storage; an upload is at most about 2 MB, so usually one.
_BLOB_GET_SIZE = 4 * 1024 * 1024


class StorageError(Exception):
    """The file is not there or cannot be read or written."""


class FileStorage(Protocol):
    async def put(self, organization_id: uuid.UUID, key: str, data: bytes) -> None:
        """Store ``data`` under ``key`` so that readers see all of it or none of it."""

    async def open(self, organization_id: uuid.UUID, key: str) -> AsyncIterator[bytes]:
        """The file's content in chunks. Raises ``StorageError`` at once when it is missing."""

    async def delete(self, organization_id: uuid.UUID, key: str) -> None:
        """Remove the file; a file that is already gone is not an error."""


def new_key() -> str:
    return secrets.token_urlsafe(32)


def _open_binary(path: Path) -> BinaryIO:
    return path.open("rb")


class LocalFileStorage:
    """Files in ``root/<practice id>/<first two characters of the key>/<key>``, written
    atomically."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, organization_id: uuid.UUID, key: str) -> Path:
        if not _KEY.fullmatch(key):
            raise StorageError("Invalid storage key.")
        return self.root / str(organization_id) / key[:2] / key

    def _write(self, organization_id: uuid.UUID, key: str, data: bytes) -> None:
        target = self._path(organization_id, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Same file system as the target, so the final rename is atomic.
        scratch = self.root / ".tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(dir=scratch, suffix=".part")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    async def put(self, organization_id: uuid.UUID, key: str, data: bytes) -> None:
        try:
            await asyncio.to_thread(self._write, organization_id, key, data)
        except OSError as exc:
            raise StorageError("The file could not be stored.") from exc

    async def open(self, organization_id: uuid.UUID, key: str) -> AsyncIterator[bytes]:
        path = self._path(organization_id, key)
        try:
            handle = await asyncio.to_thread(_open_binary, path)
        except OSError as exc:
            raise StorageError("The file is not available.") from exc
        return self._chunks(handle)

    @staticmethod
    async def _chunks(handle: BinaryIO) -> AsyncIterator[bytes]:
        try:
            while chunk := await asyncio.to_thread(handle.read, CHUNK_SIZE):
                yield chunk
        finally:
            handle.close()

    async def delete(self, organization_id: uuid.UUID, key: str) -> None:
        path = self._path(organization_id, key)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise StorageError("The file could not be removed.") from exc


class AzureBlobFileStorage:
    """Files in an Azure Blob Storage container under
    ``<practice id>/<first two characters of the key>/<key>``.

    The container stays private: the API streams every download after its own permission checks.
    The Azure SDK client used here is blocking, so each call runs in a worker thread. A file of
    this size is one Put Blob request, so readers see all of it or none of it. Azure Storage
    encrypts every blob at rest. A missing blob and any Azure failure both surface as
    ``StorageError`` (the callers already turn that into a 503 or a 404).
    """

    def __init__(self, container: Any) -> None:
        self.container = container

    def _blob_name(self, organization_id: uuid.UUID, key: str) -> str:
        if not _KEY.fullmatch(key):
            raise StorageError("Invalid storage key.")
        return f"{organization_id}/{key[:2]}/{key}"

    async def put(self, organization_id: uuid.UUID, key: str, data: bytes) -> None:
        blob_name = self._blob_name(organization_id, key)
        try:
            await asyncio.to_thread(self.container.upload_blob, blob_name, data, overwrite=True)
        except Exception as exc:
            log.warning("blob_put_failed", error=type(exc).__name__)
            raise StorageError("The file could not be stored.") from exc

    async def open(self, organization_id: uuid.UUID, key: str) -> AsyncIterator[bytes]:
        blob_name = self._blob_name(organization_id, key)
        try:
            # The first range is fetched here, so a missing blob fails at once.
            downloader = await asyncio.to_thread(self.container.download_blob, blob_name)
        except Exception as exc:
            raise StorageError("The file is not available.") from exc
        return self._chunks(downloader.chunks())

    @staticmethod
    async def _chunks(parts: Iterator[bytes]) -> AsyncIterator[bytes]:
        while (part := await asyncio.to_thread(next, parts, None)) is not None:
            for start in range(0, len(part), CHUNK_SIZE):
                yield part[start : start + CHUNK_SIZE]

    async def delete(self, organization_id: uuid.UUID, key: str) -> None:
        blob_name = self._blob_name(organization_id, key)
        try:
            await asyncio.to_thread(self.container.delete_blob, blob_name)
        except ResourceNotFoundError:
            # Already gone, like ``unlink(missing_ok=True)``.
            return
        except Exception as exc:
            raise StorageError("The file could not be removed.") from exc


async def discard(storage: FileStorage, organization_id: uuid.UUID, keys: Iterable[str]) -> None:
    """Remove a practice's files after their rows are gone (or were never committed). A file
    that cannot be removed is logged and left: it is an orphan, and nothing a caller can undo."""
    for key in keys:
        try:
            await storage.delete(organization_id, key)
        except StorageError:
            log.warning("storage_delete_failed", storage_key_prefix=key[:6])


def _blob_container(settings: Settings) -> Any:
    """The container client: a connection string when one is set (local Azurite, account keys),
    otherwise the account URL with the managed identity (``DefaultAzureCredential``)."""
    from azure.storage.blob import BlobServiceClient

    options: dict[str, Any] = {
        "connection_timeout": 5,
        "read_timeout": 30,
        "retry_total": 3,
        "max_single_get_size": _BLOB_GET_SIZE,
        "max_chunk_get_size": _BLOB_GET_SIZE,
    }
    connection_string = settings.azure_storage_connection_string.get_secret_value().strip()
    if connection_string:
        service = BlobServiceClient.from_connection_string(connection_string, **options)
    else:
        from azure.identity import DefaultAzureCredential

        service = BlobServiceClient(
            settings.azure_storage_account_url.strip(),
            credential=DefaultAzureCredential(),
            **options,
        )
    return service.get_container_client(settings.azure_storage_container.strip())


@lru_cache
def get_storage() -> FileStorage:
    settings = get_settings()
    if settings.uses_blob_storage:
        return AzureBlobFileStorage(_blob_container(settings))
    return LocalFileStorage(settings.storage_path)
