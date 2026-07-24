"""Storage backend tests: local, S3 (mocked), and local/S3 parity."""
from __future__ import annotations

import pytest

from storage_backends.base import normalize_key
from storage_backends.local import LocalStorage
from storage_backends.s3 import S3Storage
from tests.fakes import FakeS3Client


def test_normalize_key_prevents_escape():
    assert normalize_key("/../clips//x/./y.mp4") == "clips/x/y.mp4"
    assert normalize_key("a\\b\\c") == "a/b/c"


def _exercise(store):
    """Run a common sequence of operations and return observable results."""
    store.save("clips/j/a.mp4", b"hello")
    store.save("clips/j/b.txt", b"world!!")
    results = {
        "exists": store.exists("clips/j/a.mp4"),
        "missing": store.exists("clips/j/none.mp4"),
        "size": store.size("clips/j/a.mp4"),
        "read": store.open("clips/j/a.mp4").read(),
        "list": store.list("clips/j"),
    }
    store.delete("clips/j/a.mp4")
    results["exists_after_delete"] = store.exists("clips/j/a.mp4")
    return results


def test_local_storage_roundtrip(tmp_path):
    store = LocalStorage(tmp_path)
    r = _exercise(store)
    assert r["exists"] is True and r["missing"] is False
    assert r["size"] == 5
    assert r["read"] == b"hello"
    assert r["list"] == ["clips/j/a.mp4", "clips/j/b.txt"]
    assert r["exists_after_delete"] is False


def test_local_url_is_app_relative(tmp_path):
    store = LocalStorage(tmp_path)
    assert store.url("clips/j/a.mp4") == "/clips/j/a.mp4"


def test_s3_storage_roundtrip():
    store = S3Storage(client=FakeS3Client(), bucket="bkt", prefix="app")
    r = _exercise(store)
    assert r["exists"] is True and r["missing"] is False
    assert r["size"] == 5
    assert r["read"] == b"hello"
    assert r["list"] == ["clips/j/a.mp4", "clips/j/b.txt"]
    assert r["exists_after_delete"] is False


def test_s3_url_is_presigned():
    store = S3Storage(client=FakeS3Client(), bucket="bkt")
    assert store.url("clips/j/a.mp4").startswith("https://s3.example.com/")


def test_save_file_roundtrip(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload-data")
    local = LocalStorage(tmp_path / "store")
    s3 = S3Storage(client=FakeS3Client(), bucket="bkt")
    local.save_file("clips/x/v.mp4", src)
    s3.save_file("clips/x/v.mp4", src)
    assert local.open("clips/x/v.mp4").read() == b"payload-data"
    assert s3.open("clips/x/v.mp4").read() == b"payload-data"


def test_local_save_file_same_path_is_safe(tmp_path):
    """save_file must not truncate when source already lives at the destination."""
    store = LocalStorage(tmp_path)
    dest = store._path("clips/j/self.mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"already here")
    store.save_file("clips/j/self.mp4", dest)  # src == dest
    assert store.open("clips/j/self.mp4").read() == b"already here"


@pytest.mark.parametrize("op", ["save", "exists", "size", "read", "list", "delete"])
def test_local_and_s3_parity(tmp_path, op):
    """The same operations produce equivalent results on local and S3 backends."""
    local = LocalStorage(tmp_path / "local")
    s3 = S3Storage(client=FakeS3Client(), bucket="bkt", prefix="p")
    local_res = _exercise(local)
    s3_res = _exercise(s3)
    # Compare the observable outcome for the sampled operation.
    key = {
        "save": "exists", "exists": "exists", "size": "size", "read": "read",
        "list": "list", "delete": "exists_after_delete",
    }[op]
    assert local_res[key] == s3_res[key]
