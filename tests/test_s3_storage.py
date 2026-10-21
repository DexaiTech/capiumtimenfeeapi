# ruff: noqa: N803 - the fake takes the same keyword names as the boto3 client
"""The S3 file store: same layout and rules as the local one, and the setting that selects it."""

import io
import uuid

import pytest

from app.core.config import get_settings
from app.services import storage as storage_module
from app.services.storage import LocalFileStorage, S3FileStorage, StorageError, get_storage, new_key


class FakeS3:
    """Just enough of the boto3 S3 client: a dict of objects, and failures on demand."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.encryption: dict[str, str] = {}
        self.fail = False

    def put_object(self, *, Bucket, Key, Body, ServerSideEncryption=None):
        if self.fail:
            raise RuntimeError("boom")
        self.objects[f"{Bucket}/{Key}"] = Body
        self.encryption[Key] = ServerSideEncryption

    def get_object(self, *, Bucket, Key):
        if self.fail or f"{Bucket}/{Key}" not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[f"{Bucket}/{Key}"])}

    def delete_object(self, *, Bucket, Key):
        if self.fail:
            raise RuntimeError("boom")
        self.objects.pop(f"{Bucket}/{Key}", None)


@pytest.fixture
def s3() -> FakeS3:
    return FakeS3()


@pytest.fixture
def storage(s3: FakeS3) -> S3FileStorage:
    return S3FileStorage(s3, "bucket")


async def read_all(storage: S3FileStorage, org: uuid.UUID, key: str) -> bytes:
    return b"".join([chunk async for chunk in await storage.open(org, key)])


async def test_a_file_lives_under_its_practice_and_key_and_is_encrypted_at_rest(storage, s3):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"hello")
    assert list(s3.objects) == [f"bucket/{org}/{key[:2]}/{key}"]
    assert s3.encryption[f"{org}/{key[:2]}/{key}"] == "AES256"
    assert await read_all(storage, org, key) == b"hello"


async def test_big_files_are_streamed_in_chunks(storage):
    org, key = uuid.uuid4(), new_key()
    data = bytes(range(256)) * 2048
    await storage.put(org, key, data)
    chunks = [chunk async for chunk in await storage.open(org, key)]
    assert len(chunks) > 1 and b"".join(chunks) == data


async def test_another_practice_cannot_read_the_file(storage):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"secret")
    with pytest.raises(StorageError):
        await storage.open(uuid.uuid4(), key)


async def test_a_missing_file_raises_at_once(storage):
    with pytest.raises(StorageError):
        await storage.open(uuid.uuid4(), new_key())


async def test_delete_removes_the_file_and_a_gone_file_is_not_an_error(storage, s3):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"x")
    await storage.delete(org, key)
    await storage.delete(org, key)
    assert s3.objects == {}


@pytest.mark.parametrize("bad", ["../etc/passwd", "short", "a/b" * 10, ""])
async def test_a_key_that_is_not_one_of_ours_is_refused(storage, bad):
    with pytest.raises(StorageError):
        await storage.put(uuid.uuid4(), bad, b"x")


async def test_s3_failures_become_storage_errors(storage, s3):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"x")
    s3.fail = True
    with pytest.raises(StorageError):
        await storage.put(org, new_key(), b"y")
    with pytest.raises(StorageError):
        await storage.open(org, key)
    with pytest.raises(StorageError):
        await storage.delete(org, key)


def test_a_bucket_in_the_settings_selects_s3(monkeypatch):
    monkeypatch.setenv("AWS_BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    monkeypatch.setattr(storage_module, "_s3_client", lambda settings: FakeS3())
    get_settings.cache_clear()
    get_storage.cache_clear()
    try:
        chosen = get_storage()
        assert isinstance(chosen, S3FileStorage) and chosen.bucket == "my-bucket"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        get_storage.cache_clear()


def test_without_a_bucket_the_local_store_is_used():
    get_storage.cache_clear()
    assert isinstance(get_storage(), LocalFileStorage)
