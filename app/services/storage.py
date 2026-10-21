"""File storage behind a small interface (ticket attachments now, receipts later).

A file is addressed by its practice and a random *storage key* (``new_key``) that never
derives from the file's name, so nothing a client sends can reach the file system. Every call
names the practice: ``LocalFileStorage`` keeps each practice's files in a directory of their
own, so a key can only be read, replaced or removed under the practice that stored it, whatever
id or key a caller passes. The files live under one directory (``STORAGE_DIR``, see
``Settings.storage_path``). ``S3FileStorage`` keeps the same layout in an Amazon S3 bucket
(``AWS_BUCKET_NAME``); ``get_storage`` picks it when a bucket is configured.
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
from typing import Any, BinaryIO, Protocol

from app.core.config import Settings, get_settings
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


class S3FileStorage:
    """Files in an S3 bucket under ``<practice id>/<first two characters of the key>/<key>``.

    The bucket stays private: the API streams every download after its own permission checks.
    boto3 is blocking, so each call runs in a worker thread. A missing object and any S3 failure
    both surface as ``StorageError`` (the callers already turn that into a 503 or a 404).
    """

    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def _object_key(self, organization_id: uuid.UUID, key: str) -> str:
        if not _KEY.fullmatch(key):
            raise StorageError("Invalid storage key.")
        return f"{organization_id}/{key[:2]}/{key}"

    async def put(self, organization_id: uuid.UUID, key: str, data: bytes) -> None:
        object_key = self._object_key(organization_id, key)
        try:
            await asyncio.to_thread(
                self.client.put_object,
                Bucket=self.bucket,
                Key=object_key,
                Body=data,
                ServerSideEncryption="AES256",
            )
        except Exception as exc:
            log.warning("s3_put_failed", error=type(exc).__name__)
            raise StorageError("The file could not be stored.") from exc

    async def open(self, organization_id: uuid.UUID, key: str) -> AsyncIterator[bytes]:
        object_key = self._object_key(organization_id, key)
        try:
            response = await asyncio.to_thread(
                self.client.get_object, Bucket=self.bucket, Key=object_key
            )
        except Exception as exc:
            raise StorageError("The file is not available.") from exc
        return self._chunks(response["Body"])

    @staticmethod
    async def _chunks(body: Any) -> AsyncIterator[bytes]:
        try:
            while chunk := await asyncio.to_thread(body.read, CHUNK_SIZE):
                yield chunk
        finally:
            body.close()

    async def delete(self, organization_id: uuid.UUID, key: str) -> None:
        object_key = self._object_key(organization_id, key)
        try:
            # S3 answers 204 for a key that is already gone, like ``unlink(missing_ok=True)``.
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=object_key)
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


def _s3_client(settings: Settings) -> Any:
    import boto3
    from botocore.config import Config

    key_id = settings.aws_access_key_id.get_secret_value()
    secret = settings.aws_secret_access_key.get_secret_value()
    return boto3.client(
        "s3",
        region_name=settings.aws_region or None,
        aws_access_key_id=key_id or None,
        aws_secret_access_key=secret or None,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        ),
    )


@lru_cache
def get_storage() -> FileStorage:
    settings = get_settings()
    if settings.aws_bucket_name.strip():
        return S3FileStorage(_s3_client(settings), settings.aws_bucket_name.strip())
    return LocalFileStorage(settings.storage_path)
