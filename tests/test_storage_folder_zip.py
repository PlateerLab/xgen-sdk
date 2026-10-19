"""xgen_sdk.storage.folder_zip: 폴더 하나를 zip 으로.

여기서 지키는 것:

* zip 의 맨 위가 그 폴더이고, 하위 구조와 빈 폴더가 그대로 들어간다. 맨 위에서 닿지 않는 것은 없다.
* 이름이 경로를 벗어나지 않고, 한 폴더 안에서 겹치지 않는다(대소문자만 다른 것 포함, 폴더가 먼저).
* 상한을 넘으면 zip 을 만들기 전에 413 이다.
* 보내는 도중 한 파일을 읽지 못하면 나머지는 받고, 못 받은 파일은 zip 안의 안내 파일에 적힌다.
* 만든 만큼 바로 내보낸다(첫 조각이 파일을 읽기 전에 나간다).
"""

import datetime as dt
import io
import zipfile

import pytest
from fastapi import HTTPException

from xgen_sdk.storage import folder_zip


BYTES = {"a": b"# summary", "b": b"id,amount\n1,100\n", "c": b"x" * 3_000_000}


def _download(item, path):
    if item["key"] == "broken":
        raise RuntimeError("NoSuchKey")
    with open(path, "wb") as fh:
        fh.write(BYTES[item["key"]])


def _file(folder_id, name, key):
    return {"folder_id": folder_id, "name": name, "size": len(BYTES.get(key, b"")), "item": {"id": name, "key": key}}


def _zip(zplan, **kw):
    return zipfile.ZipFile(io.BytesIO(b"".join(folder_zip.stream(zplan, _download, **kw))))


def test_a_folder_becomes_a_zip_of_its_tree():
    folders = [
        {"id": "r", "parent_id": None, "name": "보고서"},
        {"id": "y", "parent_id": "r", "name": "2026"},
        {"id": "e", "parent_id": "y", "name": "빈 폴더"},
        {"id": "o", "parent_id": "elsewhere", "name": "밖"},
    ]
    files = [_file("r", "요약.md", "a"), _file("y", "매출.csv", "b"), _file("o", "밖.csv", "a")]
    zplan = folder_zip.plan_tree("r", "보고서", folders, files)
    assert zplan.download_name == "보고서.zip"
    zf = _zip(zplan)
    assert sorted(zf.namelist()) == sorted(["보고서/", "보고서/2026/", "보고서/2026/빈 폴더/", "보고서/요약.md", "보고서/2026/매출.csv"])
    assert zf.read("보고서/2026/매출.csv") == BYTES["b"]
    assert zf.testzip() is None


def test_names_stay_inside_and_do_not_collide():
    folders = [{"id": "x", "parent_id": "r", "name": "x.txt"}]
    files = [_file("r", "x.txt", "a"), _file("r", "X.TXT", "a"), _file("r", "../밖으로", "a")]
    names = _zip(folder_zip.plan_tree("r", "A", folders, files)).namelist()
    assert "A/x.txt/" in names                                     # 폴더가 먼저 이름을 갖는다
    assert {n for n in names if n.casefold().endswith(".txt")} == {"A/x (2).txt", "A/X (3).TXT"}
    assert "A/.._밖으로" in names
    assert all(n.startswith("A/") and "/../" not in f"/{n}" for n in names)


def test_limits_are_checked_before_anything_is_built():
    files = [_file("r", "a.csv", "a"), _file("r", "b.csv", "b")]
    with pytest.raises(HTTPException) as many:
        folder_zip.plan_tree("r", "A", [], files, max_files=1)
    assert many.value.status_code == 413 and "하위 폴더별로" in many.value.detail
    with pytest.raises(HTTPException) as big:
        folder_zip.plan_tree("r", "A", [], files, max_bytes=5)
    assert big.value.status_code == 413 and "너무 큽니다" in big.value.detail


def test_a_file_that_cannot_be_read_is_named_in_a_note():
    files = [_file("r", "ok.csv", "b"), _file("r", "깨진.bin", "broken")]
    zf = _zip(folder_zip.plan_tree("r", "A", [], files))
    assert zf.read("A/ok.csv") == BYTES["b"]
    assert "A/깨진.bin" not in zf.namelist()
    assert "A/깨진.bin: 원본을 읽지 못했습니다" in zf.read("A/받지 못한 파일.txt").decode()


def test_the_zip_flows_out_as_it_is_made_and_dates_come_from_the_caller():
    zplan = folder_zip.plan_tree("r", "A", [], [_file("r", "big.bin", "c")])
    chunks = folder_zip.stream(zplan, _download, chunk=256 * 1024,
                               date_of=lambda item: dt.datetime(2026, 10, 19, 9, 30, 0))
    first = next(chunks)
    assert first, "폴더 머리는 파일을 읽기 전에 나간다"
    rest = list(chunks)
    assert len(rest) > 2, "큰 파일은 여러 조각으로 나뉘어 나간다"
    zf = zipfile.ZipFile(io.BytesIO(first + b"".join(rest)))
    assert zf.read("A/big.bin") == BYTES["c"]
    assert zf.getinfo("A/big.bin").date_time == (2026, 10, 19, 9, 30, 0)
