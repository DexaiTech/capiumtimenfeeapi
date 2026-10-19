"""File storage behind a small interface (ticket attachments now, receipts later).

A file is addressed by its practice and a random *storage key* (``new_key``) that never
derives from the file's name, so nothing a client sends can reach the file system. Every call
names the practice: ``LocalFileStorage`` keeps each practice's files in a directory of their
own, so a key can only be read, replaced or removed under the practice that stored it, whatever
id or key a caller passes. The files live under one directory (``STORAGE_DIR``, see
``Settings.storage_path``); a cloud store would be another implementation of ``FileStorage``.
"""

import asyncio
import os
import re
import secrets
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterable
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

CHUNK_SIZE = 64 * 1024
_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


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


async def discard(storage: FileStorage, organization_id: uuid.UUID, keys: Iterable[str]) -> None:
    """Remove a practice's files after their rows are gone (or were never committed). A file
    that cannot be removed is logged and left: it is an orphan, and nothing a caller can undo."""
    for key in keys:
        try:
            await storage.delete(organization_id, key)
        except StorageError:
            log.warning("storage_delete_failed", storage_key_prefix=key[:6])


@lru_cache
def get_storage() -> FileStorage:
    return LocalFileStorage(get_settings().storage_path)
