"""통제 정책 공용 부분: 서비스들이 같은 뜻으로 쓰는지 못 박는다.

  1. 규칙: 전체 치환(역참조) · 부분 마스킹 · [마스킹 미적용] 다루기(skip / default) · 차례
  2. 스위치: 탐지 스위치와 자리 스위치가 둘 다 켜져야 그 자리에 쓰인다, 기본값은 예전 동작
  3. 읽기: 에이전트별 예외는 그 에이전트에서만 빠진다
  4. 파일: 형식마다 글이 있는 자리만 바뀌고(Office 는 run 이 쪼개져도), 못 읽는 형식은 그대로 둔다
  5. 로그: 종류마다 한 줄, 샘플은 잘라 남긴다
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from xgen_sdk import control_policy as cp

PHONE = {"id": 1, "policy_name": "전화번호", "regex_pattern": r"010-\d{4}-\d{4}", "masking": True,
         "masking_mode": "partial", "keep_suffix": 4, "is_active": True}
EMAIL = {"id": 2, "policy_name": "이메일", "regex_pattern": r"([\w.]+)@([\w.]+)", "masking": True,
         "masking_pattern": r"\1@[숨김]", "is_active": True}
UNMASKED = {"id": 3, "policy_name": "탐지만", "regex_pattern": r"비밀", "masking": False, "is_active": True}
SWEAR = {"id": 9, "policy_name": "욕설", "regex_pattern": r"바보", "masking": True, "masking_pattern": "**",
         "is_active": True}


def _rules(*rows, policy_type=cp.PII, unmasked="skip"):
    return cp.compile_rules(rows, policy_type, unmasked=unmasked)


# ── 1. 규칙 ──────────────────────────────────────────────────────────


def test_partial_and_full_masks():
    rules = _rules(PHONE, EMAIL)
    out = cp.apply_rules("연락처 010-1234-5678, kim.a@x.com", rules)
    assert out.text == "연락처 ***-****-5678, kim.a@[숨김]"
    assert out.total_detected == 2
    assert out.changed_policy_ids == [1, 2]
    assert cp.partial_mask("ab", keep_prefix=1, keep_suffix=1) == "**", "유지 합이 길이 이상이면 전부 가린다"


def test_unmasked_policies_are_skipped_or_failed_closed():
    assert [r.policy_id for r in _rules(PHONE, UNMASKED)] == [1], "에이전트 · 업로드 쪽은 [마스킹 미적용] 을 뺀다"
    closed = _rules(UNMASKED, dict(PHONE, masking=False), unmasked="default")
    assert cp.apply_rules("비밀 010-1234-5678", closed).text == "**** ****", "문서 서빙 쪽은 **** 로 가린다"


def test_earlier_rules_win_and_detect_only_keeps_text():
    rules = _rules(SWEAR, policy_type=cp.FORBIDDEN_WORD) + _rules(dict(SWEAR, id=10, masking_pattern="##"),
                                                                  policy_type=cp.FORBIDDEN_WORD)
    assert cp.apply_rules("바보", rules).text == "**"
    found = cp.apply_rules("바보 바보", rules, mask=False)
    assert found.text == "바보 바보" and found.total_detected == 4
    assert found.detections[0]["samples"] == [{"original": "바보", "masked": "**"}]
    assert list(found.by_type()) == [cp.FORBIDDEN_WORD]


# ── 2. 스위치 ─────────────────────────────────────────────────────────


def test_scope_settings_match_the_admin_screen():
    keys = {name: (path, default) for name, path, default, _label in cp.scope_settings()}
    assert len(keys) == 8
    assert keys["PII_BLOCK_MODEL_INPUT"] == ("guarder.piis.block_model_input", True)
    assert keys["FORBIDDEN_WORDS_STRIP_FILESTORE_UPLOAD"] == ("guarder.forbidden_words.strip_filestore_upload", False)


def test_a_scope_needs_both_switches():
    values = {"IS_AVAILABLE_PIIS": True}
    flag = lambda name, default: values.get(name, default)  # noqa: E731
    assert [k.policy_type for k in cp.kinds_on(cp.SCOPE_MODEL_INPUT, flag=flag)] == [cp.PII], "모델 자리는 기본 켜짐"
    assert cp.kinds_on(cp.SCOPE_CHAT_INPUT, flag=flag) == [], "채팅 차단은 기본 꺼짐"
    values["PII_BLOCK_CHAT_INPUT"] = True
    values["FORBIDDEN_WORDS_BLOCK_CHAT_INPUT"] = True
    assert [k.policy_type for k in cp.kinds_on(cp.SCOPE_CHAT_INPUT, flag=flag)] == [cp.PII], "금칙어 탐지가 꺼져 있다"


def test_config_flag_falls_back_to_env(monkeypatch):
    class Missing:
        def get_config_by_name(self, name):
            return type("C", (), {"value": None})()

    monkeypatch.setenv("PII_STRIP_CHAT_UPLOAD", "on")
    assert cp.config_flag("PII_STRIP_CHAT_UPLOAD", False, config=Missing()) is True
    assert cp.config_flag("PII_STRIP_FILESTORE_UPLOAD", False, config=Missing()) is False


# ── 3. 읽기 ──────────────────────────────────────────────────────────


class FakeDb:
    def __init__(self, **tables):
        self.tables = tables
        self.inserts = []

    def find_records_by_condition(self, table, cond, **_kw):
        return [r for r in self.tables.get(table, []) if all(r.get(k) == v for k, v in cond.items())]

    def execute_update_delete(self, sql, params):
        self.inserts.append(params)
        return 1


def test_agent_exceptions_only_apply_to_that_agent():
    db = FakeDb(piis=[PHONE, EMAIL, dict(UNMASKED)], agent_applied_policies=[
        {"workflow_id": "wfB", "policy_type": cp.PII, "policy_id": 1, "is_applied": False},
    ])
    flag = lambda name, default: True  # noqa: E731
    assert [r.policy_id for r in cp.load_scope_rules(db, cp.SCOPE_CHAT_INPUT, workflow_id="wfA", flag=flag)] == [1, 2]
    assert [r.policy_id for r in cp.load_scope_rules(db, cp.SCOPE_CHAT_INPUT, workflow_id="wfB", flag=flag)] == [2]
    assert [r.policy_id for r in cp.load_scope_rules(db, cp.SCOPE_FILESTORE_UPLOAD, flag=flag)] == [1, 2]


# ── 4. 파일 ──────────────────────────────────────────────────────────


def _docx(paragraph_runs):
    """run 이 쪼개진 문단을 가진 최소 docx."""
    body = "".join(
        "<w:p>" + "".join(f"<w:r><w:rPr><w:b/></w:rPr><w:t>{run}</w:t></w:r>" for run in runs) + "</w:p>"
        for runs in paragraph_runs
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="w"><w:body>{body}</w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return out.getvalue()


def test_office_runs_split_across_a_match_are_masked_and_formatting_kept():
    raw = _docx([["전화 010-12", "34-5678 입니다"], ["메일 없음"]])
    result = cp.strip_bytes(raw, "보고서.docx", _rules(PHONE))
    assert result.changed and result.hits == 1
    xml = zipfile.ZipFile(io.BytesIO(result.content)).read("word/document.xml").decode()
    assert "010-12" not in xml and "34-5678" not in xml
    assert "<w:t>전화 ***-****-5678</w:t>" in xml and "<w:t> 입니다</w:t>" in xml
    assert xml.count("<w:b/>") == 3, "run 서식은 그대로"
    assert result.found.detections[0]["policy_name"] == "전화번호"
    assert result.sha256 == hashlib.sha256(result.content).hexdigest()


def test_text_keeps_its_encoding_and_html_keeps_its_markup():
    raw = "이름,전화\n홍길동,010-1234-5678\n".encode("cp949")
    result = cp.strip_bytes(raw, "명단.csv", _rules(PHONE))
    assert result.content.decode("cp949") == "이름,전화\n홍길동,***-****-5678\n"
    html = b'<p title="010-1234-5678">010-1234-5678</p>'
    masked = cp.strip_bytes(html, "a.html", _rules(PHONE)).content.decode()
    assert masked == '<p title="010-1234-5678">***-****-5678</p>', "속성은 글이 아니다"


def test_pdf_text_is_really_removed():
    pdf = pytest.importorskip("xgen_pdf")
    doc = pdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "call 010-1234-5678 now")
    raw = doc.tobytes()
    doc.close()
    result = cp.strip_bytes(raw, "a.pdf", _rules(PHONE))
    assert result.changed
    assert "010-1234" not in cp.detection_text(result.content, "a.pdf")


def test_unreadable_files_are_left_alone(tmp_path):
    rules = _rules(PHONE)
    assert cp.strip_bytes(b"\x89PNG....", "a.png", rules).status == cp.STATUS_UNMASKABLE
    assert cp.strip_bytes(b"PK\x03\x04broken", "a.docx", rules).status == cp.STATUS_UNMASKABLE
    assert cp.strip_bytes(b"no phone", "a.txt", rules).status == cp.STATUS_CLEAN
    assert cp.strip_bytes(b"010-1234-5678", "a.txt", []).status == cp.STATUS_OFF
    path = tmp_path / "a.txt"
    path.write_bytes(b"tel 010-1234-5678")
    result = cp.strip_file(str(path), "a.txt", rules)
    assert path.read_bytes() == b"tel ***-****-5678" and result.size == path.stat().st_size
    picture = tmp_path / "b.png"
    picture.write_bytes(b"\x89PNG")
    untouched = cp.strip_file(str(picture), "b.png", rules)
    assert untouched.status == cp.STATUS_UNMASKABLE and untouched.sha256 is None, "읽지 않은 파일은 값이 없다"


# ── 5. 로그 ──────────────────────────────────────────────────────────


def test_one_typed_evidence_request_per_policy_type_without_truncating_samples():
    class Sink:
        def __init__(self):
            self.requests = []

        def ingest(self, request, *, actor):
            self.requests.append((request, actor))
            return cp.EvidenceIngestResult("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", len(self.requests), ())

    db = FakeDb(workflow_meta=[{"id": 31, "workflow_id": "wfA"}])
    sink = Sink()
    rules = _rules(PHONE) + _rules(SWEAR, policy_type=cp.FORBIDDEN_WORD)
    found = cp.apply_rules("바보 010-1234-5678 " + "x" * 200, rules, mask=False)
    assert cp.log_result(db, found, source=cp.SOURCE_CHAT_INPUT, workflow_id="wfA", user_id=7,
                         evidence_sink=sink) == 2
    requests = {request.event.policy_type: request for request, _actor in sink.requests}
    assert set(requests) == {cp.PII, cp.FORBIDDEN_WORD}
    assert requests[cp.PII].event.source == "chat_input"
    assert requests[cp.PII].subject_type == "workflow" and requests[cp.PII].subject_id == 31
    assert requests[cp.PII].event.user_id == 7
    assert db.inserts == [], "SDK evidence producers may read subject identity but never write the database"
    evidence = requests[cp.FORBIDDEN_WORD].detections
    assert json.loads(evidence) == [{"policy_id": 9, "policy_name": "욕설", "count": 1,
                                     "samples": [], "enforced": True}]
    exact = cp.build_detections([{"samples": [{"original": "a" * 300, "masked": ""}]}])
    assert exact.wire()[0]["samples"][0]["original"] == "a" * 300
    assert "a" * 101 not in str(exact) and "a" * 101 not in repr(exact)


# ── 검토에서 재현된 것들 (2026-10-13) ───────────────────────────────

LOOKBEHIND = {"id": 20, "policy_name": "비밀번호", "regex_pattern": r"(?<=password: )\S+", "masking": True,
              "masking_pattern": "[숨김]", "is_active": True}


def test_lookaround_patterns_are_masked_in_office_and_spans():
    rules = _rules(LOOKBEHIND)
    assert cp.apply_rules("password: hunter2", rules).text == "password: [숨김]"
    raw = _docx([["password: hun", "ter2"]])
    result = cp.strip_bytes(raw, "a.docx", rules)
    xml = zipfile.ZipFile(io.BytesIO(result.content)).read("word/document.xml").decode()
    assert result.changed and "hunter2" not in xml.replace("<w:t>", "").replace("</w:t>", "") and "hun" not in xml


def test_spreadsheet_cells_are_not_joined():
    sheet = ('<worksheet><sheetData><row><c t="inlineStr"><is><t>kim@x.com</t></is></c>'
             '<c t="inlineStr"><is><t>010-1234-5678</t></is></c></row></sheetData></worksheet>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    for rules in (_rules(EMAIL, PHONE), _rules(PHONE, EMAIL)):
        xml = zipfile.ZipFile(io.BytesIO(cp.strip_bytes(out.getvalue(), "a.xlsx", rules).content)).read(
            "xl/worksheets/sheet1.xml").decode()
        assert "<t>kim@[숨김]</t>" in xml and "<t>***-****-5678</t>" in xml, xml


def test_office_entities_survive_and_untouched_nodes_stay_byte_identical():
    raw = _docx([["say &quot;hi&quot; &#8220;q&#8221; ", "010-1234-5678"], ["keep &amp;quot;"]])
    xml = zipfile.ZipFile(io.BytesIO(cp.strip_bytes(raw, "a.docx", _rules(PHONE)).content)).read(
        "word/document.xml").decode()
    assert "<w:t>say &quot;hi&quot; &#8220;q&#8221; </w:t>" in xml, "걸리지 않은 노드는 원문 그대로"
    assert "<w:t>keep &amp;quot;</w:t>" in xml and "&amp;amp;" not in xml


def test_html_keeps_markup_charset_and_comments():
    page = '<html><head><meta charset="euc-kr"></head><body><br><!-- 담당 010-1234-5678 -->&nbsp;010-1234-5678</body></html>'
    raw = page.encode("euc-kr")
    out = cp.strip_bytes(raw, "a.html", _rules(PHONE)).content.decode("euc-kr")
    assert out == page.replace("010-1234-5678", "***-****-5678"), "태그 · charset · <br> · &nbsp; 그대로, 주석은 주석으로"


def test_text_round_trips_unknown_bytes_and_korean_cp949():
    weird = b"\x00\x01\xff\xfe 010-1234-5678 \x80\x81"
    assert cp.strip_bytes(weird, "a.log", _rules(PHONE)).content == b"\x00\x01\xff\xfe ***-****-5678 \x80\x81"
    korean = "메모: 010-1234-5678".encode("cp949")
    masked = cp.strip_bytes(korean, "a.txt", _rules(dict(PHONE, masking_mode="full", masking_pattern="[전화]")))
    assert masked.content.decode("cp949") == "메모: [전화]", "짧은 cp949 글을 라틴 문자로 읽어 깨뜨리지 않는다"


def test_names_without_extension_are_not_formats():
    assert cp.masker_for("m") is None and cp.masker_for("pdf") is None
    assert cp.masker_for("report.PDF").name == "pdf" and cp.masker_for_extension("docx").name == "ooxml"
    assert cp.strip_bytes(b"\x89 010-1234-5678", "m", _rules(PHONE)).status == cp.STATUS_UNMASKABLE


def test_office_zip_bomb_is_refused(monkeypatch):
    from xgen_sdk.control_policy import files

    monkeypatch.setattr(files, "MASKING_MAX_UNZIPPED_BYTES", 1000)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:p><w:t>" + "a" * 5000 + "</w:t></w:p>")
    result = cp.strip_bytes(out.getvalue(), "a.docx", _rules(PHONE))
    assert result.status == cp.STATUS_UNMASKABLE and "상한" in result.reason


def test_pdf_output_is_deterministic_and_thread_safe():
    pdf = pytest.importorskip("xgen_pdf")
    import threading

    doc = pdf.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f"call 010-1234-{5000 + i} now")
    raw = doc.tobytes()
    doc.close()
    first = cp.strip_bytes(raw, "a.pdf", _rules(PHONE))
    assert first.changed and cp.strip_bytes(raw, "a.pdf", _rules(PHONE)).sha256 == first.sha256, "같은 입력은 같은 바이트"
    results = []
    threads = [threading.Thread(target=lambda: results.append(cp.strip_bytes(raw, "a.pdf", _rules(PHONE)).sha256))
               for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [first.sha256] * 6


def test_broken_backreference_fails_closed():
    rules = _rules(dict(PHONE, masking_mode="full", masking_pattern=r"\1"))
    assert cp.apply_rules("010-1234-5678", rules).text == "****"
    assert cp.apply_rules("010-1234-5678", rules, mask=False).detections[0]["samples"][0]["masked"] == "****"


def test_bad_size_env_does_not_break_import():
    import subprocess
    import sys

    code = "import xgen_sdk.control_policy as c; print(c.MASKING_MAX_BYTES)"
    env = {**__import__("os").environ, "DOCUMENT_SOURCE_MASKING_MAX_BYTES": "32MB"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and out.stdout.strip() == str(32 * 1024 * 1024)


# ── 2차 검토에서 재현된 것들 (2026-10-13) ────────────────────────────


def _docx_text(content: bytes) -> str:
    import re

    xml = zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml").decode()
    return "".join(re.findall(r"<w:t>([^<]*)</w:t>", xml))


def test_chained_rules_give_the_same_text_in_every_format():
    phone = dict(PHONE, masking_mode="full", masking_pattern="[전화]")
    chained = {"id": 30, "policy_name": "번호 묶음", "regex_pattern": r"\[전화\] 번호", "masking": True,
               "masking_pattern": "[가림]", "is_active": True}
    rules = _rules(phone, chained)
    assert cp.apply_rules("전화 010-1234-5678 번호 끝", rules).text == "전화 [가림] 끝"
    docx = cp.strip_bytes(_docx([["전화 010-1234-5678 번", "호 끝"]]), "a.docx", rules)
    assert _docx_text(docx.content) == "전화 [가림] 끝", "앞 마스크 위에서 다시 걸린 것도 Office 에서 같은 결과"
    spans = cp.find_masked_spans("전화 010-1234-5678 번호 끝", rules)
    assert [(s, e, r) for s, e, r in spans] == [(3, 19, "[가림]")]


def test_html_entities_text_brackets_and_masks_are_handled():
    rules = _rules(dict(PHONE, masking_mode="full", masking_pattern="<전화>"))
    page = "<p>010&#45;1234&#45;5678 and a < b 010-1234-5678</p><script-x>010-1234-5678</script-x>"
    out = cp.strip_bytes(page.encode(), "a.html", rules).content.decode()
    assert out == ("<p>&lt;전화&gt; and a < b &lt;전화&gt;</p><script-x>&lt;전화&gt;</script-x>"), out
    assert "010" not in cp.detection_text(out.encode(), "a.html")


def test_text_decoding_keeps_utf32_and_mostly_utf8_korean():
    utf32 = "메모 010-1234-5678".encode("utf-32")
    assert cp.strip_bytes(utf32, "a.txt", _rules(PHONE)).content.decode("utf-32") == "메모 ***-****-5678"
    mostly = "가나다라마바사 메모 010-1234-5678 ".encode() * 20 + b"\xff"
    out = cp.strip_bytes(mostly, "a.txt", _rules(PHONE)).content
    assert out.endswith(b"\xff") and out[:-1].decode() == "가나다라마바사 메모 ***-****-5678 " * 20


def test_office_comments_deleted_text_and_prefixed_parts_are_masked():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", '<w:document><w:p><w:del><w:r><w:delText>010-1234-5678</w:delText>'
                                        '</w:r></w:del></w:p></w:document>')
        z.writestr("xl/comments1.xml", '<comments><commentList><comment ref="A1"><text><r><t>010-1234-5678</t>'
                                       '</r></text></comment></commentList></comments>')
        z.writestr("xl/sharedStrings.xml", '<x:sst><x:si><x:t>010-1234-5678</x:t></x:si></x:sst>')
    result = cp.strip_bytes(out.getvalue(), "a.xlsx", _rules(PHONE))
    assert result.hits == 3
    with zipfile.ZipFile(io.BytesIO(result.content)) as z:
        assert all("010-1234" not in z.read(name).decode() for name in z.namelist())


def test_office_zip_bomb_counts_media_too(monkeypatch):
    from xgen_sdk.control_policy import files

    monkeypatch.setattr(files, "MASKING_MAX_UNZIPPED_BYTES", 1000)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:p><w:t>010-1234-5678</w:t></w:p>")
        z.writestr("word/media/image1.png", b"\x00" * 5000)
    result = cp.strip_bytes(out.getvalue(), "a.docx", _rules(PHONE))
    assert result.status == cp.STATUS_UNMASKABLE and "상한" in result.reason


def test_rule_repl_with_a_broken_template_fails_closed():
    rule = _rules(dict(PHONE, masking_mode="full", masking_pattern=r"\2"))[0]
    assert rule.pattern.sub(rule.repl, "tel 010-1234-5678") == "tel ****", "서비스가 re.sub 에 바로 넘겨도 안전"


def test_pdf_runs_in_a_separate_process_and_recovers():
    pytest.importorskip("xgen_pdf")
    import os

    from xgen_sdk.control_policy import files

    if files._PDF_WORKERS <= 0:
        pytest.skip("작업 프로세스를 끈 환경")
    pdf = __import__("xgen_pdf")
    doc = pdf.open()
    doc.new_page().insert_text((72, 72), "call 010-1234-5678 now")
    raw = doc.tobytes()
    doc.close()
    first = cp.strip_bytes(raw, "a.pdf", _rules(PHONE))
    workers = list(files._PDF_IDLE)
    assert workers and all(w.process.pid != os.getpid() for w in workers)
    for worker in workers:
        worker.kill()
    again = cp.strip_bytes(raw, "a.pdf", _rules(PHONE))
    assert again.changed and again.sha256 == first.sha256, "죽은 작업 프로세스는 새로 띄운다"


# ── 3차 검토 (2026-10-13) ────────────────────────────────────────────


def test_mostly_ascii_cp949_is_still_read_as_cp949():
    raw = ("번호,값\n" + "".join(f"{i},{i * 7}\n" for i in range(300)) + "비고: 홍길동\n").encode("cp949")
    rule = {"id": 5, "policy_name": "이름", "regex_pattern": "홍길동", "masking": True, "masking_pattern": "[이름]"}
    out = cp.strip_bytes(raw, "a.csv", _rules(rule))
    assert out.changed and out.content.decode("cp949").endswith("비고: [이름]\n")


def test_empty_matches_hide_nothing_and_break_nothing():
    rules = _rules({"id": 6, "policy_name": "숫자", "regex_pattern": r"\d*", "masking": True})
    assert cp.apply_rules("a1b22c", rules).text == "a****b****c"
    assert rules[0].pattern.sub(rules[0].repl, "a1b") == "a****b"


def test_large_html_with_one_hit_stays_light():
    import sys

    resource = pytest.importorskip("resource")

    page = "<pre>" + "일반 로그 줄 R&amp;D " * 250000 + "연락 010-1234-5678</pre>"
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out = cp.strip_bytes(page.encode(), "big.html", _rules(PHONE))
    # getrusage reports bytes on Darwin and KiB on Linux.
    rss_units_per_mb = 1024 * 1024 if sys.platform == "darwin" else 1024
    grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / rss_units_per_mb
    assert out.hits == 1 and out.content.decode().endswith("연락 ***-****-5678</pre>")
    assert grown_mb < 300, f"글자마다 표를 만들지 않는다 (+{grown_mb:.0f} MB)"


# ── 한글 문서(HWP 5.0 · HWPX) ───────────────────────────────────────

import struct  # noqa: E402
import zlib  # noqa: E402

from xgen_sdk.control_policy import _cfb, _hwp  # noqa: E402

FIELD_START = struct.pack("<H", 3) + b"klh%" + b"\0" * 8 + struct.pack("<H", 3)  # 하이퍼링크 필드 시작(8 글자)
FIELD_END = struct.pack("<H", 4) + b"\0" * 12 + struct.pack("<H", 4)
PARA_END = struct.pack("<H", 13)


def _raw_deflate(data: bytes) -> bytes:
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    return packer.compress(data) + packer.flush()


def _record(tag: int, level: int, payload: bytes) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), size) + payload
    return struct.pack("<I", tag | (level << 10) | (size << 20)) + payload


def _paragraph(text: bytes, shapes) -> bytes:
    chars = len(text) // 2
    header = struct.pack("<IIHBBHHHIH", chars | 0x80000000, 0, 0, 0, 0, len(shapes), 1, 1, 0, 0)
    return (_record(66, 0, header) + _record(67, 1, text)
            + _record(68, 1, b"".join(struct.pack("<II", p, s) for p, s in shapes))
            + _record(69, 1, struct.pack("<IiiiiiiiI", 0, 0, 1000, 1000, 850, 600, 0, 42520, 0x60000))
            + _record(70, 1, struct.pack("<III", 0, chars, 1 << 24)))


def _summary(author: str) -> bytes:
    value = (author + "\0").encode("utf-16-le")
    props = [struct.pack("<IH", 2, 1200) + b"\0\0", struct.pack("<II", 0x1F, len(value) // 2) + value]
    props[1] += b"\0" * (-len(props[1]) % 4)
    start = 8 + 2 * 8
    table = struct.pack("<II", 1, start) + struct.pack("<II", 4, start + len(props[0]))
    section = struct.pack("<II", start + sum(map(len, props)), 2) + table + b"".join(props)
    return struct.pack("<HHI", 0xFFFE, 0, 0) + b"\0" * 16 + struct.pack("<I", 1) + b"\x11" * 16 + struct.pack("<I", 48) + section


def _hwp_doc(body: bytes, *, flags: int = 1, prv: str = "", author: str = "", history: bool = False) -> bytes:
    pack = _raw_deflate if flags & 1 else (lambda d: d)
    children = [
        _cfb.Entry("FileHeader", 2, data=b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000107, flags)
                   + b"\0" * 216),
        _cfb.Entry("DocInfo", 2, data=pack(b"")),
        _cfb.Entry("BodyText", 1, children=[_cfb.Entry("Section0", 2, data=pack(body))]),
        _cfb.Entry("PrvText", 2, data=prv.encode("utf-16-le")),
        _cfb.Entry("PrvImage", 2, data=b"GIF89a" + b"\0" * 40),
        _cfb.Entry("\x05HwpSummaryInformation", 2, data=_summary(author)),
    ]
    if history:
        children.append(_cfb.Entry("DocHistory", 1, children=[_cfb.Entry("VersionLog0", 2, data=b"010-1234-5678")]))
    return _cfb.write(_cfb.Entry("Root Entry", 5, children=children))


def _section_records(content: bytes):
    root = _cfb.read(content)
    return root, _hwp._records(zlib.decompress(root.find("BodyText/Section0").data, -15))


def test_hwp_body_preview_summary_and_history_are_masked():
    first = "전화 ".encode("utf-16-le") + FIELD_START + "010-1234-5678".encode("utf-16-le") + FIELD_END \
        + " 끝".encode("utf-16-le") + PARA_END
    second = "다른 문단".encode("utf-16-le") + PARA_END
    raw = _hwp_doc(_paragraph(first, [(0, 0), (11, 1)]) + _paragraph(second, [(0, 0)]),
                   prv="전화 010-1234-5678 끝", author="홍길동", history=True)
    assert "010-1234-5678" in cp.detection_text(raw, "a.hwp")
    rules = _rules(PHONE, {"id": 7, "policy_name": "이름", "regex_pattern": "홍길동", "masking": True,
                           "masking_pattern": "[이름]"})
    result = cp.strip_bytes(raw, "보고서.hwp", rules)
    assert result.changed and result.hits == 2, "본문 1 + 작성자 1(미리보기는 본문을 옮긴 글이라 세지 않는다)"
    assert cp.detection_text(result.content, "a.hwp") == "전화 ***-****-5678 끝\n다른 문단\n[이름]", "요약 정보 글도"
    root, records = _section_records(result.content)
    assert root.find("PrvText").data.decode("utf-16-le") == "전화 ***-****-5678 끝"
    assert root.find("PrvImage").data.startswith(b"\x89PNG"), "첫 쪽 그림은 빈 그림으로"
    assert "홍길동" not in root.find("\x05HwpSummaryInformation").data.decode("utf-16-le", "ignore")
    assert root.find("DocHistory") is None and not struct.unpack_from("<I", root.find("FileHeader").data, 36)[0] & 0x40
    header, text, shapes = records[0], records[1], records[2]
    assert text.payload.count(FIELD_START) == 1 and text.payload.count(FIELD_END) == 1 and text.payload.endswith(PARA_END)
    assert struct.unpack_from("<I", header.payload, 0)[0] == 0x80000000 | len(text.payload) // 2, "문단 글자 수"
    assert [r.tag for r in records[:4]] == [66, 67, 68, 70] and struct.unpack_from("<H", header.payload, 16)[0] == 0, \
        "고친 문단의 줄 배치 캐시는 지운다(한컴이 다시 계산)"
    assert list(struct.iter_unpack("<II", shapes.payload)) == [(0, 0), (11, 1)]
    assert struct.unpack_from("<II", records[3].payload)[1] == len(text.payload) // 2, "영역 태그 끝도 새 글자 수로"
    untouched = _hwp._records(zlib.decompress(_cfb.read(raw).find("BodyText/Section0").data, -15))[5:]
    assert [r.raw for r in records[4:]] == [r.raw for r in untouched], "걸리지 않은 문단은 바이트 그대로"


def test_hwp_controls_inside_a_match_stay_in_place():
    text = "g".encode("utf-16-le") + FIELD_START + "mail 끝".encode("utf-16-le") + PARA_END
    raw = _hwp_doc(_paragraph(text, [(0, 0)]))
    rules = _rules({"id": 8, "policy_name": "메일", "regex_pattern": "gmail", "masking": True,
                    "masking_mode": "partial", "keep_prefix": 1})
    _, records = _section_records(cp.strip_bytes(raw, "a.hwp", rules).content)
    assert records[1].payload == "g".encode("utf-16-le") + FIELD_START + "**** 끝".encode("utf-16-le") + PARA_END, \
        "필드 시작은 매치 안 같은 자리(링크가 덮는 범위가 옮겨 가지 않게)"


def test_locked_or_old_hwp_is_left_alone():
    body = _paragraph("010-1234-5678".encode("utf-16-le") + PARA_END, [(0, 0)])
    rules = _rules(PHONE)
    for flags, reason in ((0x3, "암호"), (0x5, "배포용"), (0x11, "DRM")):
        result = cp.strip_bytes(_hwp_doc(body, flags=flags), "a.hwp", rules)
        assert result.status == cp.STATUS_UNMASKABLE and reason in result.reason
    assert "97" in cp.strip_bytes(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05", "a.hwp", rules).reason
    assert cp.strip_bytes(b"not a document", "a.hwp", rules).status == cp.STATUS_UNMASKABLE
    plain = cp.strip_bytes(_hwp_doc(body, flags=0), "a.hwp", rules)
    assert plain.changed and "010-1234" not in cp.detection_text(plain.content, "a.hwp"), "압축하지 않은 문서도"


def _hwpx(section: str, *, creator: str = "", preview: str = "") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip")  # 첫 항목 · 무압축
        z.writestr("Contents/header.xml", '<hh:head xmlns:hh="h"><hh:docInfo/></hh:head>')
        z.writestr("Contents/section0.xml", '<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="s" xmlns:hp="p">'
                   + section + "</hs:sec>")
        z.writestr("Contents/content.hpf", '<opf:package xmlns:opf="o"><opf:metadata><opf:meta name="creator" '
                   f'content="text">{creator}</opf:meta></opf:metadata></opf:package>')
        z.writestr("Preview/PrvText.txt", preview.encode("utf-8"))
        z.writestr("Preview/PrvImage.png", b"\x89PNG old")
    return out.getvalue()


LINESEG = '<hp:linesegarray><hp:lineseg textpos="0" vertpos="0"/></hp:linesegarray>'


def test_hwpx_paragraphs_tables_and_layout_cache():
    section = (
        f'<hp:p id="1"><hp:run><hp:t>전화 010-12<hp:tab width="4000"/>34-5678 &amp; 끝</hp:t></hp:run>{LINESEG}</hp:p>'
        f'<hp:p id="2"><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList><hp:p id="3"><hp:run><hp:t>홍길동</hp:t></hp:run>'
        f'{LINESEG}</hp:p></hp:subList></hp:tc></hp:tr></hp:tbl><hp:t>표 뒤 글</hp:t></hp:run>{LINESEG}</hp:p>'
    )
    raw = _hwpx(section, creator="홍길동", preview="전화 010-1234-5678")
    rules = _rules(PHONE, {"id": 7, "policy_name": "이름", "regex_pattern": "홍길동", "masking": True,
                           "masking_pattern": "<이름>"})
    result = cp.strip_bytes(raw, "a.hwpx", rules)
    assert result.changed and result.hits == 3
    with zipfile.ZipFile(io.BytesIO(result.content)) as z:
        assert [i.filename for i in z.infolist()][0] == "mimetype" and z.infolist()[0].compress_type == zipfile.ZIP_STORED
        xml = z.read("Contents/section0.xml").decode()
        assert '<hp:t>전화 ***-**<hp:tab width="4000"/>**-5678 &amp; 끝</hp:t>' in xml, "탭은 같은 비율 자리에"
        assert "<hp:t>&lt;이름&gt;</hp:t>" in xml
        assert xml.count("<hp:linesegarray>") == 1 and "표 뒤 글</hp:t></hp:run>" + LINESEG in xml, \
            "바뀐 문단(1 · 3)의 줄 배치 캐시만 지운다"
        assert "홍길동" not in z.read("Contents/content.hpf").decode()
        assert z.read("Preview/PrvText.txt").decode() == "전화 ***-****-5678"
        assert z.read("Preview/PrvImage.png") != b"\x89PNG old"
    assert cp.detection_text(result.content, "a.hwpx") == "전화 ***-****-5678 & 끝\n표 뒤 글\n<이름>\n<이름>", "작성자도"
    as_hwp = cp.strip_bytes(raw, "zip으로_저장된.hwp", rules)
    assert as_hwp.changed and as_hwp.content == result.content, "확장자가 hwp 여도 zip 이면 HWPX 로"


def test_compound_file_round_trip_keeps_streams_and_attributes():
    big = bytes(range(256)) * (9 * 1024 * 1024 // 256)  # FAT 이 109 섹터를 넘어 DIFAT 이 필요한 크기
    root = _cfb.Entry("Root Entry", 5, clsid=b"\x01" * 16, children=[
        _cfb.Entry("Big", 2, data=big),
        _cfb.Entry("Small", 2, data=b"tiny"),
        _cfb.Entry("Empty", 2),
        _cfb.Entry("Store", 1, clsid=b"\x02" * 16, created=b"\x03" * 8, modified=b"\x04" * 8,
                   children=[_cfb.Entry(f"S{i}", 2, data=bytes([i]) * (i * 700)) for i in range(12)]),
    ])
    again = _cfb.read(_cfb.write(root))
    assert {p: e.data for p, e in again.streams()} == {p: e.data for p, e in root.streams()}
    store = again.find("Store")
    assert (store.clsid, store.created, store.modified, again.clsid) == (b"\x02" * 16, b"\x03" * 8, b"\x04" * 8, b"\x01" * 16)
    olefile = pytest.importorskip("olefile")
    ole = olefile.OleFileIO(_cfb.write(root))
    assert ole.openstream("Big").read() == big and ole.openstream("Store/S11").read() == bytes([11]) * 7700


def test_office_document_properties_are_masked():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>본문</w:t></w:r></w:p></w:document>")
        z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:dc="d" xmlns:cp="c"><dc:creator>홍길동</dc:creator>'
                                        "<dc:title>보고서</dc:title></cp:coreProperties>")
    rules = _rules({"id": 7, "policy_name": "이름", "regex_pattern": "홍길동", "masking": True, "masking_pattern": "[이름]"})
    result = cp.strip_bytes(out.getvalue(), "a.docx", rules)
    core = zipfile.ZipFile(io.BytesIO(result.content)).read("docProps/core.xml").decode()
    assert result.hits == 1 and "<dc:creator>[이름]</dc:creator>" in core and "<dc:title>보고서</dc:title>" in core


def test_hwp_short_paragraph_headers_from_older_versions():
    text = "전화 010-1234-5678".encode("utf-16-le") + PARA_END
    chars = len(text) // 2
    body = (_record(66, 0, struct.pack("<IIHBBHH", chars, 0, 0, 0, 0, 1, 0))  # 5.0.0.x: 16 바이트 머리
            + _record(67, 1, text) + _record(68, 1, struct.pack("<II", 0, 0)))
    result = cp.strip_bytes(_hwp_doc(body), "old.hwp", _rules(PHONE))
    _, records = _section_records(result.content)
    assert result.changed and len(records[0].payload) == 16
    assert struct.unpack_from("<I", records[0].payload, 0)[0] == len(records[1].payload) // 2


def _hlk(command: str) -> bytes:
    body = command.encode("utf-16-le")
    return (b"klh%" + struct.pack("<IB", 0, 0) + struct.pack("<H", len(body) // 2) + body + struct.pack("<I", 7))


def test_hwp_text_outside_paragraphs_is_masked_and_positions_stay_ordered():
    # 하이퍼링크 명령(mailto) · 개체 설명(그림 원래 파일 이름) · 위치가 섞인 문단
    text = "hong".encode("utf-16-le") + FIELD_START + "@example.com".encode("utf-16-le") + FIELD_END + PARA_END
    para = _paragraph(text, [(0, 0), (4, 1), (13, 2)])
    gso = (b" osg" + b"\0" * 40 + struct.pack("<H", 9) + "kim@x.com".encode("utf-16-le") + b"\0" * 4)
    body = para + _record(71, 1, _hlk("mailto\\:hong@example.com;1;0;0;")) + _record(71, 1, gso)
    rules = _rules({"id": 9, "policy_name": "메일", "regex_pattern": r"\w+@[\w.]+", "masking": True,
                    "masking_pattern": "[EMAIL]"})
    result = cp.strip_bytes(_hwp_doc(body), "a.hwp", rules)
    _, records = _section_records(result.content)
    assert result.hits == 3 and "@" not in cp.detection_text(result.content, "a.hwp")
    shapes = [p for p, _ in struct.iter_unpack("<II", records[2].payload)]
    assert shapes == sorted(shapes) and shapes[0] == 0, "글자 모양 위치가 뒤집히지 않는다"
    start, end, _ = struct.unpack("<III", records[3].payload)
    assert start <= end == len(records[1].payload) // 2
    assert records[4].payload[11:-4].decode("utf-16-le") == "mailto\\:[EMAIL]*********;1;0;0;", \
        "링크 주소도(필드 명령은 같은 길이로: 누름틀처럼 값마다 길이를 적어 두는 명령이 어긋나지 않게)"
    assert records[4].payload.endswith(struct.pack("<I", 7)) and "[EMAIL]" in records[5].payload.decode("utf-16-le", "ignore")


def test_hwp_bombs_history_and_cut_previews():
    from xgen_sdk.control_policy import files

    clean = _paragraph("안녕하세요".encode("utf-16-le") + PARA_END, [(0, 0)])
    only_history = cp.strip_bytes(_hwp_doc(clean, history=True), "a.hwp", _rules(PHONE))
    assert only_history.changed and _cfb.read(only_history.content).find("DocHistory") is None, \
        "본문이 깨끗해도 이력에 걸린 것이 있으면 이력을 지운다"
    phone = _paragraph("전화 010-1234-5678".encode("utf-16-le") + PARA_END, [(0, 0)])
    cut = cp.strip_bytes(_hwp_doc(phone, prv="전화 010-1234-56"), "a.hwp", _rules(PHONE))
    assert _cfb.read(cut.content).find("PrvText").data.decode("utf-16-le") == "전화 ***-****-56", \
        "매치 한가운데서 잘린 미리보기도 고친 본문으로 다시 만든다"
    bomb = _hwp_doc(_paragraph(("a" * 200000).encode("utf-16-le") + PARA_END, [(0, 0)]))
    original = files.MASKING_MAX_UNZIPPED_BYTES
    files.MASKING_MAX_UNZIPPED_BYTES = 100000
    try:
        refused = cp.strip_bytes(bomb, "a.hwp", _rules(PHONE))
    finally:
        files.MASKING_MAX_UNZIPPED_BYTES = original
    assert refused.status == cp.STATUS_UNMASKABLE and "상한" in refused.reason


def test_hwp_unreadable_summary_with_a_hit_is_dropped():
    raw = _hwp_doc(_paragraph("본문".encode("utf-16-le") + PARA_END, [(0, 0)]))
    root = _cfb.read(raw)
    root.find("\x05HwpSummaryInformation").data = b"\0\0" + "작성자 010-1234-5678".encode("utf-16-le")
    result = cp.strip_bytes(_cfb.write(root), "a.hwp", _rules(PHONE))
    assert result.changed and _cfb.read(result.content).find("\x05HwpSummaryInformation") is None


def test_hwpx_strings_cdata_and_duplicate_entries():
    section = ('<hp:p><hp:run><hp:ctrl><hp:fieldBegin type="HYPERLINK"><hp:parameters>'
               '<hp:stringParam name="Command">mailto\\:kim@x.com;1;0;0;</hp:stringParam></hp:parameters>'
               '</hp:fieldBegin></hp:ctrl><hp:t a="1>2"><![CDATA[메일 kim@x.com]]></hp:t></hp:run></hp:p>'
               '<hp:pic><hp:shapeComment>사진 kim@x.com.png</hp:shapeComment></hp:pic>')
    rules = _rules({"id": 9, "policy_name": "메일", "regex_pattern": r"\w+@[\w]+\.com", "masking": True,
                    "masking_pattern": "[EMAIL]"})
    result = cp.strip_bytes(_hwpx(section), "a.hwpx", rules)
    xml = zipfile.ZipFile(io.BytesIO(result.content)).read("Contents/section0.xml").decode()
    assert result.hits == 3 and "kim@" not in xml
    assert "<![CDATA[메일 [EMAIL]]]>" in xml and "mailto\\:[EMAIL]**;1;0;0;" in xml, "필드 매개변수는 같은 길이로"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Contents/section0.xml", "<hp:p><hp:run><hp:t>kim@x.com</hp:t></hp:run></hp:p>")
        with pytest.warns(UserWarning):
            z.writestr("Contents/section0.xml", "<hp:p/>")
    assert cp.strip_bytes(out.getvalue(), "a.hwpx", rules).status == cp.STATUS_UNMASKABLE, "이름이 겹친 항목"


def test_office_typed_properties_are_left_alone_and_thumbnails_blanked():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>2024-01-02</w:t></w:r></w:p></w:document>")
        z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:dcterms="t"><dcterms:created>2024-01-02T03:04:05Z'
                                        "</dcterms:created><cp:revision>12345678</cp:revision></cp:coreProperties>")
        z.writestr("docProps/thumbnail.jpeg", b"\xff\xd8 first page")
        z.writestr("_rels/.rels", '<Relationships><Relationship Id="rId1" Type="t/thumbnail" '
                                  'Target="docProps/thumbnail.jpeg"/><Relationship Id="rId2" Type="d" '
                                  'Target="word/document.xml"/></Relationships>')
        z.writestr("[Content_Types].xml", '<Types><Default Extension="jpeg" ContentType="image/jpeg"/>'
                                          '<Override PartName="/docProps/thumbnail.jpeg" ContentType="image/jpeg"/></Types>')
    rules = _rules({"id": 3, "policy_name": "날짜", "regex_pattern": r"\d{4}-\d{2}-\d{2}|\d{8}", "masking": True,
                    "masking_pattern": "[생일]"})
    result = cp.strip_bytes(out.getvalue(), "a.docx", rules)
    z = zipfile.ZipFile(io.BytesIO(result.content))
    assert result.hits == 1 and "2024-01-02T03:04:05Z" in z.read("docProps/core.xml").decode()
    assert "12345678" in z.read("docProps/core.xml").decode()
    assert "docProps/thumbnail.jpeg" not in z.namelist(), "첫 쪽을 그린 썸네일은 관계와 함께 뺀다"
    assert "thumbnail" not in z.read("_rels/.rels").decode() and "word/document.xml" in z.read("_rels/.rels").decode()
    assert "thumbnail" not in z.read("[Content_Types].xml").decode()


def test_html_script_and_style_contents_are_masked_without_breaking_them():
    page = '<p>x</p><script type="a>b">var p = "010-1234-5678";</script><style>/* 010-1234-5678 */</style>'
    rules = _rules(dict(PHONE, masking_mode="full", masking_pattern="</script>"))
    out = cp.strip_bytes(page.encode(), "a.html", rules).content.decode()
    assert out == ('<p>x</p><script type="a>b">var p = "<\\/script>";</script><style>/* <\\/script> */</style>'), out
    titled = '<p title="a>b 010-1234-5678">본문 010-1234-5678</p>'
    assert cp.strip_bytes(titled.encode(), "a.html", _rules(PHONE)).content.decode() == \
        '<p title="a>b 010-1234-5678">본문 ***-****-5678</p>', "속성 값 안의 > 는 태그 끝이 아니다(속성은 그대로)"


# ── 3차 검토 (한글 · HTML) ───────────────────────────────────────────


def test_html_apostrophes_in_text_do_not_hide_it():
    rules = _rules(PHONE)
    for page in ("<p data-x=it's>a</p><p>전화 010-1234-5678</p><p>that's</p>",
                 "if a<b then it's x> 010-1234-5678 and that's"):
        out = cp.strip_bytes(page.encode(), "a.html", rules)
        assert out.changed and "010-1234" not in out.content.decode(), page


def test_hwp_clickhere_commands_keep_their_length_and_bad_streams_are_refused():
    command = "Clickhere:set:22:Direction:wstring:22:담당자 kim@x.com 에게 문의 Help:wstring:0: "
    body = _paragraph("본문".encode("utf-16-le") + PARA_END, [(0, 0)]) + _record(71, 1, _hlk(command))
    rules = _rules({"id": 9, "policy_name": "메일", "regex_pattern": r"\w+@[\w.]+\w", "masking": True,
                    "masking_pattern": "[EMAIL]"})
    _, records = _section_records(cp.strip_bytes(_hwp_doc(body), "a.hwp", rules).content)
    assert records[5].tag == 71 and records[5].payload[11:-4].decode("utf-16-le") == command.replace("kim@x.com", "[EMAIL]**")
    raw = _hwp_doc(body)
    root = _cfb.read(raw)
    section = root.find("BodyText/Section0")
    section.data = section.data[:-4]  # 잘린 압축
    assert cp.strip_bytes(_cfb.write(root), "a.hwp", rules).status == cp.STATUS_UNMASKABLE


def test_hwp_unreadable_history_is_dropped_and_previews_are_rechecked():
    clean = _paragraph("안녕하세요".encode("utf-16-le") + PARA_END, [(0, 0)])
    root = _cfb.read(_hwp_doc(clean, history=True))
    root.find("DocHistory/VersionLog0").data = b"\x00not deflate at all"
    result = cp.strip_bytes(_cfb.write(root), "a.hwp", _rules(PHONE))
    assert result.changed and _cfb.read(result.content).find("DocHistory") is None, "볼 수 없는 이력은 지운다"
    name = {"id": 4, "policy_name": "이름", "regex_pattern": r"이름:\s*\S+", "masking": True, "masking_pattern": "[이름]"}
    body = _paragraph("이름: 홍길동".encode("utf-16-le") + PARA_END, [(0, 0)]) \
        + _paragraph("전화 010-1234-5678".encode("utf-16-le") + PARA_END, [(0, 0)])
    out = cp.strip_bytes(_hwp_doc(body, prv="이름:\r\n홍길동 전화 010-1234-5678"), "a.hwp", _rules(PHONE, name))
    preview = _cfb.read(out.content).find("PrvText").data.decode("utf-16-le")
    assert "홍길동" not in preview and "010-1234" not in preview


def test_hwpx_cdata_in_string_leaves_and_metadata():
    section = '<hp:p><hp:run><hp:t>본문</hp:t></hp:run></hp:p><hp:pic><hp:shapeComment><![CDATA[kim@x.com]]></hp:shapeComment></hp:pic>'
    raw = _hwpx(section, creator="<![CDATA[lee@y.com]]>")
    rules = _rules({"id": 9, "policy_name": "메일", "regex_pattern": r"\w+@\w+\.com", "masking": True,
                    "masking_pattern": "[EMAIL]"})
    result = cp.strip_bytes(raw, "a.hwpx", rules)
    z = zipfile.ZipFile(io.BytesIO(result.content))
    assert result.hits == 2 and "@" not in z.read("Contents/section0.xml").decode() + z.read("Contents/content.hpf").decode()


# ── 최종 검토 ────────────────────────────────────────────────────────


def test_summary_binary_values_are_not_read_as_text():
    raw = _hwp_doc(_paragraph("hello".encode("utf-16-le") + PARA_END, [(0, 0)]), author="kim")
    root = _cfb.read(raw)
    summary = bytearray(root.find("\x05HwpSummaryInformation").data)
    # 시각(VT_FILETIME) 값이 UTF-16 으로 읽으면 한글("가각")이 되는 바이트
    filetime = struct.pack("<I", 0x40) + "가각".encode("utf-16-le") + b"\0" * 4
    section_at = 48
    size, count = struct.unpack_from("<II", summary, section_at)
    table_end = section_at + 8 + count * 8
    body = bytes(summary[table_end:section_at + size])
    entries = [struct.unpack_from("<II", summary, section_at + 8 + i * 8) for i in range(count)]
    shift = 8
    table = b"".join(struct.pack("<II", pid, off + shift) for pid, off in entries)
    table += struct.pack("<II", 12, 8 + (count + 1) * 8 + len(body))
    section = struct.pack("<II", 8 + (count + 1) * 8 + len(body) + len(filetime), count + 1) + table + body + filetime
    root.find("\x05HwpSummaryInformation").data = bytes(summary[:section_at]) + section
    korean_name = _rules({"id": 4, "policy_name": "이름", "regex_pattern": "[가-힣]{2,4}", "masking": True})
    result = cp.strip_bytes(_cfb.write(root), "a.hwp", korean_name)
    assert result.status == cp.STATUS_CLEAN, "시각 바이트를 글로 읽어 깨끗한 문서를 고치지 않는다"


def test_hwpx_cdata_does_not_run_across_elements_and_previews_recheck():
    section = ('<hp:p><hp:run><hp:t><![CDATA[a]]><hp:tab/>b</hp:t></hp:run></hp:p>'
               '<hp:pic><hp:shapeComment>kim@x.com</hp:shapeComment></hp:pic>'
               '<hp:p><hp:run><hp:t><![CDATA[c]]></hp:t></hp:run></hp:p>')
    mail = _rules({"id": 9, "policy_name": "메일", "regex_pattern": r"\w+@\w+\.com", "masking": True,
                   "masking_pattern": "[EMAIL]"})
    result = cp.strip_bytes(_hwpx(section), "a.hwpx", mail)
    assert result.changed and "kim@" not in zipfile.ZipFile(io.BytesIO(result.content)).read("Contents/section0.xml").decode()
    name = {"id": 4, "policy_name": "이름", "regex_pattern": r"이름:\s*\S+", "masking": True, "masking_pattern": "[이름]"}
    body = ('<hp:p><hp:run><hp:t>이름:</hp:t></hp:run></hp:p>'
            '<hp:p><hp:run><hp:t>홍길동 전화 010-1234-5678</hp:t></hp:run></hp:p>')
    out = cp.strip_bytes(_hwpx(body, preview="이름:\r\n홍길동 전화 010-1234-5678"), "a.hwpx", _rules(PHONE, name))
    preview = zipfile.ZipFile(io.BytesIO(out.content)).read("Preview/PrvText.txt").decode()
    assert "홍길동" not in preview and "010-1234" not in preview


def test_same_length_counts_utf16_units_and_odd_html_attributes():
    rules = _rules({"id": 4, "policy_name": "이름", "regex_pattern": "홍.길동", "masking": True, "masking_pattern": "😀😀😀"})
    masked, hits = _hwp._mask_same_length("담당 홍😀길동 끝", rules)
    assert hits == 1 and _hwp._utf16_units(masked) == _hwp._utf16_units("담당 홍😀길동 끝")
    page = '<a href=/q?a= "x>010-1234-5678">'
    assert "010-1234" not in cp.strip_bytes(page.encode(), "a.html", _rules(PHONE)).content.decode(), \
        "따옴표 없는 값 뒤의 따옴표는 값이 아니다(브라우저에 보이는 글)"


def test_office_thumbnail_relationship_in_single_quotes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>010-1234-5678</w:t></w:r></w:p></w:document>")
        z.writestr("docProps/thumbnail.jpeg", b"\xff\xd8 first page")
        z.writestr("_rels/.rels", "<Relationships><Relationship Id='r1' Target = '/DocProps/thumbnail.jpeg' Type='t'>"
                                  "</Relationship></Relationships>")
    z = zipfile.ZipFile(io.BytesIO(cp.strip_bytes(out.getvalue(), "a.docx", _rules(PHONE)).content))
    assert "docProps/thumbnail.jpeg" not in z.namelist() and "thumbnail" not in z.read("_rels/.rels").decode()
