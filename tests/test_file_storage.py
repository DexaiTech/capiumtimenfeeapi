"""The file storage: tenant-scoped, atomic, keyed by random keys; and the setting that places it."""

import uuid
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT, get_settings
from app.services.storage import LocalFileStorage, StorageError, discard, new_key


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "files"


@pytest.fixture
def storage(root: Path) -> LocalFileStorage:
    return LocalFileStorage(root)


async def read_all(storage: LocalFileStorage, org: uuid.UUID, key: str) -> bytes:
    return b"".join([chunk async for chunk in await storage.open(org, key)])


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


# --- Keys and layout ------------------------------------------------------------------------


def test_new_keys_are_long_random_and_url_safe():
    keys = {new_key() for _ in range(200)}
    assert len(keys) == 200
    assert all(len(k) == 43 and k.replace("-", "").replace("_", "").isalnum() for k in keys)


async def test_a_file_lives_in_its_practices_folder_under_its_key(storage, root):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"hello")
    assert files_under(root) == [root / str(org) / key[:2] / key]
    assert await read_all(storage, org, key) == b"hello"


async def test_big_files_are_streamed_in_chunks(storage):
    org, key = uuid.uuid4(), new_key()
    data = bytes(range(256)) * 2048  # 512 KB
    await storage.put(org, key, data)
    chunks = [chunk async for chunk in await storage.open(org, key)]
    assert len(chunks) > 1 and b"".join(chunks) == data


async def test_putting_a_key_again_replaces_the_file_whole(storage, root):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"first version")
    await storage.put(org, key, b"v2")
    assert await read_all(storage, org, key) == b"v2"
    assert len(files_under(root)) == 1


async def test_no_scratch_file_is_left_behind(storage, root):
    org = uuid.uuid4()
    for _ in range(3):
        await storage.put(org, new_key(), b"data")
    assert [p for p in files_under(root) if p.suffix == ".part"] == []
    assert list((root / ".tmp").iterdir()) == []


# --- Tenancy --------------------------------------------------------------------------------


async def test_a_key_is_only_reachable_under_the_practice_that_stored_it(storage):
    mine, theirs, key = uuid.uuid4(), uuid.uuid4(), new_key()
    await storage.put(mine, key, b"secret")
    with pytest.raises(StorageError):
        await storage.open(theirs, key)
    # Removing it under the wrong practice removes nothing.
    await storage.delete(theirs, key)
    assert await read_all(storage, mine, key) == b"secret"
    # The same key under two practices is two files.
    await storage.put(theirs, key, b"theirs")
    assert await read_all(storage, mine, key) == b"secret"
    assert await read_all(storage, theirs, key) == b"theirs"
    await storage.delete(mine, key)
    assert await read_all(storage, theirs, key) == b"theirs"


# --- Delete ---------------------------------------------------------------------------------


async def test_deleting_removes_the_file_and_a_missing_file_is_no_error(storage):
    org, key = uuid.uuid4(), new_key()
    await storage.put(org, key, b"x")
    await storage.delete(org, key)
    await storage.delete(org, key)
    with pytest.raises(StorageError):
        await storage.open(org, key)


async def test_discard_removes_several_and_survives_a_failure(storage, root, monkeypatch):
    org = uuid.uuid4()
    keys = [new_key() for _ in range(3)]
    for key in keys:
        await storage.put(org, key, b"x")
    real_delete = storage.delete

    async def flaky(organization_id, key):
        if key == keys[0]:
            raise StorageError("read-only")
        await real_delete(organization_id, key)

    monkeypatch.setattr(storage, "delete", flaky)
    await discard(storage, org, keys)  # the first fails, the others are still removed
    assert [p.name for p in files_under(root)] == [keys[0]]
    await discard(storage, org, [])


# --- Hostile keys ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "",
        "short",
        "../../etc/passwd",
        "..\\..\\windows",
        "a/b" + "c" * 20,
        "a" * 20 + "/../x",
        "has space " + "a" * 20,
        "x" * 20 + "\x00",
        "x" * 20 + ".png",
        "x" * 129,
        "\u00e9" * 20,
    ],
)
async def test_a_key_that_is_not_one_of_ours_never_touches_the_file_system(storage, root, key):
    org = uuid.uuid4()
    with pytest.raises(StorageError):
        await storage.put(org, key, b"x")
    with pytest.raises(StorageError):
        await storage.open(org, key)
    with pytest.raises(StorageError):
        await storage.delete(org, key)
    assert not root.exists() or files_under(root) == []


# --- Failure --------------------------------------------------------------------------------


async def test_a_store_that_cannot_write_raises_storage_error(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the folder should be")
    broken = LocalFileStorage(blocker)
    with pytest.raises(StorageError):
        await broken.put(uuid.uuid4(), new_key(), b"x")


# --- The setting ----------------------------------------------------------------------------


def test_the_tests_use_their_own_temporary_directory_not_the_repos_storage():
    path = get_settings().storage_path
    assert path.is_absolute() and "capium-test-storage-" in path.name
    assert PROJECT_ROOT / "storage" not in (path, *path.parents)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("", PROJECT_ROOT / "storage"),
        ("   ", PROJECT_ROOT / "storage"),
        ("uploads", PROJECT_ROOT / "uploads"),
        ("var/files", PROJECT_ROOT / "var" / "files"),
    ],
)
def test_the_storage_directory_setting(configured, expected):
    settings = get_settings().model_copy(update={"storage_dir": configured})
    assert settings.storage_path == expected


def test_an_absolute_storage_directory_is_used_as_it_is(tmp_path):
    settings = get_settings().model_copy(update={"storage_dir": str(tmp_path / "mounted")})
    assert settings.storage_path == tmp_path / "mounted"
