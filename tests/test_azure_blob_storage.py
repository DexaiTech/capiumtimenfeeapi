"""The Azure Blob file store: same layout and rules as the local one, and the settings that
select it."""

import uuid
from collections.abc import Iterator

import pytest
from azure.core.exceptions import ResourceNotFoundError
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.services import storage as storage_module
from app.services.storage import (
    CHUNK_SIZE,
    AzureBlobFileStorage,
    LocalFileStorage,
    StorageError,
    get_storage,
    new_key,
)


class FakeDownloader:
    def __init__(self, data: bytes, part_size: int) -> None:
        self.data = data
        self.part_size = part_size

    def chunks(self) -> Iterator[bytes]:
        for start in range(0, len(self.data), self.part_size):
            yield self.data[start : start + self.part_size]


class FakeContainer:
    """Just enough of the Azure ``ContainerClient``: a dict of blobs, and failures on demand."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.overwrites: list[bool] = []
        self.fail = False
        # Ranged GETs larger than one streamed chunk, like the real client's 4 MB ranges.
        self.part_size = CHUNK_SIZE * 3

    def upload_blob(self, name: str, data: bytes, *, overwrite: bool = False) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.overwrites.append(overwrite)
        self.blobs[name] = data

    def download_blob(self, name: str) -> FakeDownloader:
        if self.fail:
            raise RuntimeError("boom")
        if name not in self.blobs:
            raise ResourceNotFoundError("BlobNotFound")
        return FakeDownloader(self.blobs[name], self.part_size)

    def delete_blob(self, name: str) -> None:
        if self.fail:
            raise RuntimeError("boom")
        if name not in self.blobs:
            raise ResourceNotFoundError("BlobNotFound")
        del self.blobs[name]


@pytest.fixture
def container() -> FakeContainer:
    return FakeContainer()


@pytest.fixture
def storage(container: FakeContainer) -> AzureBlobFileStorage:
    return AzureBlobFileStorage(container)


async def read_all(storage: AzureBlobFileStorage, org: uuid.UUID, key: str) -> bytes:
    return b"".join([chunk async for chunk in await storage.open(org, key)])


async def test_a_file_lives_under_its_practice_and_key(storage, container):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"hello")
    assert list(container.blobs) == [f"{org}/{key[:2]}/{key}"]
    assert container.overwrites == [True]
    assert await read_all(storage, org, key) == b"hello"


async def test_big_files_are_streamed_in_chunks(storage):
    org, key = uuid.uuid4(), new_key()
    data = bytes(range(256)) * 2048
    await storage.put(org, key, data)
    chunks = [chunk async for chunk in await storage.open(org, key)]
    assert len(chunks) > 1 and b"".join(chunks) == data
    assert max(len(chunk) for chunk in chunks) <= CHUNK_SIZE


async def test_another_practice_cannot_read_the_file(storage):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"secret")
    with pytest.raises(StorageError):
        await storage.open(uuid.uuid4(), key)


async def test_a_missing_file_raises_at_once(storage):
    with pytest.raises(StorageError):
        await storage.open(uuid.uuid4(), new_key())


async def test_delete_removes_the_file_and_a_gone_file_is_not_an_error(storage, container):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"x")
    await storage.delete(org, key)
    await storage.delete(org, key)
    assert container.blobs == {}


@pytest.mark.parametrize("bad", ["../etc/passwd", "short", "a/b" * 10, ""])
async def test_a_key_that_is_not_one_of_ours_is_refused(storage, bad):
    with pytest.raises(StorageError):
        await storage.put(uuid.uuid4(), bad, b"x")


async def test_azure_failures_become_storage_errors(storage, container):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"x")
    container.fail = True
    with pytest.raises(StorageError):
        await storage.put(org, new_key(), b"y")
    with pytest.raises(StorageError):
        await storage.open(org, key)
    with pytest.raises(StorageError):
        await storage.delete(org, key)


def test_a_container_in_the_settings_selects_blob_storage(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_CONTAINER", "uploads")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://capium.blob.core.windows.net")
    fake = FakeContainer()
    monkeypatch.setattr(storage_module, "_blob_container", lambda settings: fake)
    get_settings.cache_clear()
    get_storage.cache_clear()
    try:
        chosen = get_storage()
        assert isinstance(chosen, AzureBlobFileStorage) and chosen.container is fake
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        get_storage.cache_clear()


def test_a_container_without_a_credential_is_refused(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_CONTAINER", "uploads")
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    with pytest.raises(ValidationError, match="AZURE_STORAGE_CONTAINER needs"):
        Settings()


def test_a_connection_string_builds_a_client_for_the_container():
    settings = Settings(
        azure_storage_container="uploads",
        azure_storage_connection_string="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
        "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;",
    )
    container = storage_module._blob_container(settings)
    assert container.container_name == "uploads"


def test_without_a_container_the_local_store_is_used():
    get_storage.cache_clear()
    assert isinstance(get_storage(), LocalFileStorage)
