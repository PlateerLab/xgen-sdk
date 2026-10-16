"""xgen_sdk.storage.transfer — 큰 파일을 메모리에 올리지 않고 받고 내보내기.

지키는 것:
* 업로드를 크기 없이(통째로) 읽지 않는다 — 1MiB 단위.
* 상한을 넘으면 413, 임시 파일은 남지 않는다(선언 크기로 먼저, 없거나 틀려도 읽는 중에).
* 내보내기는 디스크의 임시 파일을 흘려 보내고 전송이 끝나면 지운다. 한글 파일명은 RFC 5987.
"""

import asyncio
import hashlib
import io
import os
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from xgen_sdk.storage import transfer as tr


class _StrictUpload(UploadFile):
    async def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        assert size and size > 0, "업로드를 통째로 메모리에 읽었다"
        return await super().read(size)


def _upload(data: bytes, *, declare: bool = True, name: str = "a.bin") -> _StrictUpload:
    return _StrictUpload(file=io.BytesIO(data), filename=name, size=len(data) if declare else None,
                         headers=Headers({"content-type": "application/octet-stream"}))


def _leftovers(prefix):
    return sorted(Path(tempfile.gettempdir()).glob(prefix + "*"))


def test_default_limit_is_one_gib(monkeypatch):
    monkeypatch.delenv(tr.UPLOAD_MAX_BYTES_ENV, raising=False)
    assert tr.upload_max_bytes() == 1024 ** 3
    monkeypatch.setenv(tr.UPLOAD_MAX_BYTES_ENV, "2048")
    assert tr.upload_max_bytes() == 2048
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv(tr.UPLOAD_MAX_BYTES_ENV, bad)
        assert tr.upload_max_bytes() == 1024 ** 3


def test_spooled_moves_to_disk_hashes_and_cleans_up():
    data = os.urandom(3 * 1024 * 1024 + 5)
    digest = hashlib.sha256()

    async def run():
        async with tr.spooled(_upload(data), 8 * 1024 * 1024, digest=digest, prefix="t-sdk-spool-") as (path, size):
            assert size == len(data)
            assert Path(path).read_bytes() == data
            return path

    path = asyncio.run(run())
    assert not Path(path).exists()
    assert digest.hexdigest() == hashlib.sha256(data).hexdigest()


def test_limit_is_enforced_while_reading_without_a_declared_size():
    before = _leftovers("t-sdk-limit-")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(tr.spool(_upload(b"x" * 4096, declare=False), 1024, prefix="t-sdk-limit-"))
    assert caught.value.status_code == 413
    assert _leftovers("t-sdk-limit-") == before, "넘친 임시 파일이 남았다"


def test_declared_size_is_rejected_before_reading():
    up = _upload(b"tiny")
    up.size = 10 ** 12
    with pytest.raises(HTTPException) as caught:
        asyncio.run(tr.spool(up, 1024, too_large="큽니다"))
    assert caught.value.status_code == 413 and caught.value.detail == "큽니다"


def test_write_chunks_into_an_open_file(tmp_path):
    data = os.urandom(2 * 1024 * 1024 + 1)
    target = tmp_path / "out.bin"
    digest = hashlib.sha256()

    async def run():
        with open(target, "wb") as out:
            return await tr.write_chunks(_upload(data), out, len(data), digest=digest)

    assert asyncio.run(run()) == len(data)
    assert target.read_bytes() == data and digest.hexdigest() == hashlib.sha256(data).hexdigest()


def test_spooled_tolerates_a_moved_file(tmp_path):
    async def run():
        async with tr.spooled(_upload(b"abc")) as (path, _size):
            os.replace(path, tmp_path / "kept.bin")

    asyncio.run(run())
    assert (tmp_path / "kept.bin").read_bytes() == b"abc"


def test_content_disposition_is_latin1_safe():
    header = tr.content_disposition("보고서 최종.pdf", "inline")
    header.encode("latin-1")
    assert header == "inline; filename*=UTF-8''%EB%B3%B4%EA%B3%A0%EC%84%9C%20%EC%B5%9C%EC%A2%85.pdf"
    assert tr.content_disposition("a.txt", "weird").startswith("attachment;")


def test_temp_file_response_streams_and_removes_the_file(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    data = os.urandom(5 * 1024 * 1024)
    src = tmp_path / "big.bin"
    src.write_bytes(data)
    app = FastAPI()

    @app.get("/f")
    def f():
        return tr.temp_file_response(str(src), filename="큰 파일.bin", media_type="application/x-test")

    r = TestClient(app).get("/f")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"].startswith("application/x-test")
    assert r.headers["content-length"] == str(len(data))
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    assert not src.exists(), "전송이 끝나면 임시 파일을 지운다"


def test_storage_file_response_downloads_and_decrypts(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from xgen_sdk.storage import minio_client

    calls = {}

    def fake_download(client, object_name, destination_path, bucket_name="documents", decrypt=True):
        calls["args"] = (object_name, bucket_name, decrypt)
        Path(destination_path).write_bytes(b"plain")

    monkeypatch.setattr(minio_client, "download_file", fake_download)
    app = FastAPI()

    @app.get("/f")
    async def f():
        return await tr.storage_file_response(object(), "u/1/a.pdf", bucket_name="filestore",
                                              filename="a.pdf", media_type="application/pdf", disposition="inline")

    r = TestClient(app).get("/f")
    assert r.status_code == 200 and r.content == b"plain"
    assert calls["args"] == ("u/1/a.pdf", "filestore", True)
    assert r.headers["content-disposition"].startswith("inline;")


def test_storage_file_response_cleans_up_when_download_fails(monkeypatch):
    from xgen_sdk.storage import minio_client

    def boom(*a, **k):
        raise RuntimeError("missing")

    monkeypatch.setattr(minio_client, "download_file", boom)
    before = _leftovers("t-sdk-dl-")
    with pytest.raises(RuntimeError):
        asyncio.run(tr.storage_file_response(object(), "x", bucket_name="b", prefix="t-sdk-dl-"))
    assert _leftovers("t-sdk-dl-") == before
