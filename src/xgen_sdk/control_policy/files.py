"""파일 바이트 가리기: 형식마다 글이 있는 자리만 바꾸고 나머지 바이트는 그대로 둔다.

쓰는 곳:

    xgen-documents  출처 원본 서빙(컬렉션 문서 미리보기) · 파일 저장소 업로드 제거
    xgen-workflow   채팅 파일 업로드 제거(작업 공간 · 고정본 대화 · 배포 API · Teams 첨부)

    형식                                   방법
    txt · md · csv · json · 코드 등         원래 인코딩으로 되돌아가게 읽고 글 전체에 적용
    html · htm · xhtml                     태그 밖 글과 주석만(태그 · 속성 · 스크립트 · 스타일은 바이트 그대로)
    docx · xlsx · pptx (+ 매크로 변형)      문단 · 셀(w:p · a:p · si · is)마다 텍스트 노드를 이어 찾고,
                                           걸린 노드만 다시 쓴다(나머지 노드는 원문 그대로)
    pdf                                    페이지 글에서 찾아 좌표로 redaction(글자를 실제로 지운다)

글을 읽을 수 없는 형식(그림 · 한글 · 옛 Office · 압축 등), 텍스트 레이어가 없는 PDF(스캔), 암호 PDF,
상한(:data:`MASKING_MAX_BYTES`, 압축을 푼 Office 는 :data:`MASKING_MAX_UNZIPPED_BYTES`)을 넘는 파일은
:class:`MaskingUnsupportedError`. 그때 어떻게 할지(원본 거절 · 그대로 둠)는 부르는 쪽이 정한다.

같은 입력 · 같은 규칙이면 같은 바이트가 나온다(PDF 문서 ID 도 입력에서 만든다). 업로드 쪽은 저장 키와
같은 내용 판정을 sha256 으로 하므로, 같은 파일을 다시 올렸을 때 다른 바이트가 나오면 안 된다.

무거운 의존(xgen-pdf · chardet)은 함수 안에서 늦게 불러온다: 이 SDK 를 쓰는 서비스가 다 가진 것은 아니다
(core 는 파일을 가리지 않는다). 없으면 그 형식만 :class:`MaskingUnsupportedError`. PDF 엔진(pdfium)은 스레드에
안전하지 않아 한 프로세스 안에서 한 번에 하나씩만 돈다.
"""
from __future__ import annotations

import codecs
import hashlib
import html as html_lib
import io
import os
import pickle
import re
import struct
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape

from .rules import MaskingResult, MaskingRule, apply_rules, find_masked_spans


def _int_env(names: Sequence[str], default: int) -> int:
    for name in names:
        raw = os.getenv(name)
        if raw:
            try:
                return int(raw)
            except ValueError:
                continue
    return default


#: 가리려면 파일 전체를 메모리에 올려야 해서 상한을 둔다. 옛 이름(documents)도 읽는다.
MASKING_MAX_BYTES = _int_env(("XGEN_FILE_MASKING_MAX_BYTES", "DOCUMENT_SOURCE_MASKING_MAX_BYTES"), 32 * 1024 * 1024)
#: Office 파일은 압축을 푼 크기로도 막는다(작은 zip 이 수십 GB 로 풀리는 파일). 그림 등 모든 항목을 더한다.
MASKING_MAX_UNZIPPED_BYTES = _int_env(("XGEN_FILE_MASKING_MAX_UNZIPPED_BYTES",), 256 * 1024 * 1024)
#: PDF 를 가리는 별도 프로세스 수 · 한 파일 시간 상한(초). 0 이면 이 프로세스 안에서(시험용).
_PDF_WORKERS = _int_env(("XGEN_FILE_MASKING_PDF_WORKERS",), 2)
_PDF_TIMEOUT_S = _int_env(("XGEN_FILE_MASKING_PDF_TIMEOUT_S",), 300)


class MaskingUnsupportedError(Exception):
    """이 파일을 가릴 수단이 없다(형식 · 스캔본 · 암호 · 상한)."""

    def __init__(self, extension: str, reason: str = "지원하지 않는 형식"):
        self.extension = extension
        self.reason = reason
        super().__init__(f"'{extension or '알 수 없음'}' 형식은 가릴 수 없습니다 ({reason}).")



def extension_of(file_name: str) -> str:
    """파일 이름의 확장자(소문자). 점이 없으면 빈 값: 이름 ``m`` 이 확장자 ``m`` 으로 읽히지 않게."""
    base = os.path.basename(file_name or "")
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


# ── 글 형식 ──────────────────────────────────────────────────────────

#: (BOM, 인코딩). UTF-32 LE 의 BOM 은 UTF-16 LE BOM 으로 시작하므로 먼저 본다.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
)
#: 이 비율 아래로만 깨진 UTF-8 은 UTF-8 로 읽는다(깨진 바이트는 그대로 싣는다). chardet 은 이 확신 이상만 믿는다.
_UTF8_BAD_RATIO = 0.01
_CHARDET_CONFIDENCE = 0.8


@dataclass(frozen=True)
class _Decoded:
    text: str
    encoding: str
    bom: bytes = b""
    errors: str = "strict"


def _decode(raw: bytes) -> _Decoded:
    """원래 바이트로 **되돌아가게** 읽는다. 맞는 인코딩을 못 찾으면 모르는 바이트를 그대로 싣는다.

    중요한 것은 정확한 인코딩보다 되돌아감이다: 걸린 자리만 바뀌고 나머지 바이트는 그대로여야 한다. 차례는
    BOM, UTF-8(깨진 바이트가 드물면 그것만 그대로 싣고 UTF-8 로), cp949(짧은 cp949 글을 chardet 은 라틴 문자로
    짐작한다), 확신이 높은 chardet 짐작, 끝으로 UTF-8 + 모르는 바이트 그대로.
    """
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            try:
                return _Decoded(raw[len(bom):].decode(encoding), encoding, bom)
            except UnicodeDecodeError:
                break
    try:
        return _Decoded(raw.decode("utf-8"), "utf-8")
    except UnicodeDecodeError:
        loose = raw.decode("utf-8", errors="surrogateescape")
        bad = sum(1 for ch in loose if "\udc80" <= ch <= "\udcff")
        if bad <= max(1, int(len(loose) * _UTF8_BAD_RATIO)):
            return _Decoded(loose, "utf-8", errors="surrogateescape")
    candidates = ["cp949"]
    try:
        import chardet

        guess = chardet.detect(raw[:64 * 1024])
        detected = guess.get("encoding")
        if detected and (guess.get("confidence") or 0) >= _CHARDET_CONFIDENCE and detected.lower() not in candidates:
            candidates.append(detected)
    except Exception:  # noqa: BLE001, chardet 이 없으면 cp949 만
        pass
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
            if text.encode(encoding) == raw:
                return _Decoded(text, encoding)
        except (UnicodeDecodeError, UnicodeEncodeError, LookupError):
            continue
    # 어느 인코딩도 아니다(섞인 바이너리 등): 못 읽는 바이트는 surrogateescape 로 실어 그대로 되돌린다.
    return _Decoded(raw.decode("utf-8", errors="surrogateescape"), "utf-8", errors="surrogateescape")


def _encode(text: str, decoded: _Decoded) -> bytes:
    try:
        body = text.encode(decoded.encoding, errors=decoded.errors)
    except UnicodeEncodeError:
        # 마스크 글자가 이 인코딩에 없다(라틴 문서에 한글 마스크 등). 원래 글은 이 인코딩에서 왔으니
        # 마스크 글자만 ? 로 바뀐다. 파일 인코딩을 통째로 바꾸지 않는다.
        body = text.encode(decoded.encoding, errors="replace")
    return decoded.bom + body


def mask_text(text: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    result = apply_rules(text, rules)
    return result.text, result.total_detected


def _mask_plain_text(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    decoded = _decode(raw)
    masked, hits = mask_text(decoded.text, rules)
    return (_encode(masked, decoded) if hits else raw), hits


# ── HTML ─────────────────────────────────────────────────────────────

#: 스크립트 · 스타일 시작 태그(이름 뒤가 공백 · / · > 일 때만: <script-x> 같은 사용자 요소는 아니다).
_HTML_RAW_TEXT = re.compile(r"<(script|style)(?=[\s/>])", re.I)
#: 문자 참조(이름 · 10진 · 16진, 세미콜론까지).
_HTML_ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")

#: 구간 종류. TEXT 는 문자 참조를 풀어 보고, RAW(주석 · CDATA 안)는 그대로 보고, MARKUP(태그 · 스크립트 · 스타일)은 보지 않는다.
_TEXT, _RAW, _MARKUP = "text", "raw", "markup"


def _html_segments(document: str) -> List[Tuple[int, int, str]]:
    """``(시작, 끝, 종류)`` 구간들. 한 번 훑는다(닫히지 않은 주석 · 스크립트가 많아도 다시 훑지 않는다).

    ``<`` 는 뒤가 글자 · ``/`` · ``!`` · ``?`` 일 때만 태그로 본다(``a < b`` 의 ``<`` 는 글이다).
    """
    segments: List[Tuple[int, int, str]] = []
    pos, text_start, n = 0, 0, len(document)

    def flush(end: int) -> None:
        if end > text_start:
            segments.append((text_start, end, _TEXT))

    while pos < n:
        lt = document.find("<", pos)
        if lt < 0:
            break
        nxt = document[lt + 1:lt + 2]
        if not (nxt.isascii() and (nxt.isalpha() or nxt in "/!?")):
            pos = lt + 1
            continue
        flush(lt)
        for opener, closer in (("<!--", "-->"), ("<![CDATA[", "]]>")):
            if document.startswith(opener, lt):
                close = document.find(closer, lt + len(opener))
                end = n if close < 0 else close
                segments.append((lt, lt + len(opener), _MARKUP))
                segments.append((lt + len(opener), end, _RAW))
                if close >= 0:
                    segments.append((close, close + len(closer), _MARKUP))
                pos = n if close < 0 else close + len(closer)
                break
        else:
            raw = _HTML_RAW_TEXT.match(document, lt)
            if raw:
                closing = re.compile(r"</" + raw.group(1) + r"\s*>", re.I).search(document, raw.end())
                end = n if closing is None else closing.end()
            else:
                gt = document.find(">", lt)
                end = n if gt < 0 else gt + 1
            segments.append((lt, end, _MARKUP))
            pos = end
        text_start = pos
    flush(n)
    return segments


def _mask_html_text(segment: str, rules: Sequence[MaskingRule], *, decode_entities: bool) -> Tuple[str, int]:
    """글 구간 하나. 문자 참조(``R&amp;D`` · ``&#48;10``)를 풀어 찾고, 걸린 구간을 원문 위치(참조 통째)로 되짚어
    이스케이프한 마스크로 바꾼다(마스크 ``<숨김>`` 이 태그가 되지 않게)."""
    if decode_entities and "&" in segment:
        decoded_parts: List[str] = []
        raw_of: List[Tuple[int, int]] = []
        pos = 0
        for entity in _HTML_ENTITY.finditer(segment):
            for k in range(pos, entity.start()):
                raw_of.append((k, k + 1))
            decoded_parts.append(segment[pos:entity.start()])
            value = html_lib.unescape(entity.group(0))
            if value == entity.group(0):
                for k in range(entity.start(), entity.end()):
                    raw_of.append((k, k + 1))
            else:
                raw_of.extend([(entity.start(), entity.end())] * len(value))
            decoded_parts.append(value)
            pos = entity.end()
        for k in range(pos, len(segment)):
            raw_of.append((k, k + 1))
        decoded_parts.append(segment[pos:])
        decoded = "".join(decoded_parts)
    else:
        decoded, raw_of = segment, None
    spans = find_masked_spans(decoded, rules)
    if not spans:
        return segment, 0
    out: List[str] = []
    last = 0
    for start, end, mask in spans:
        r_start, r_end = (raw_of[start][0], raw_of[end - 1][1]) if raw_of is not None else (start, end)
        if r_start < last:  # 한 참조가 두 구간에 걸친 드문 경우: 앞 구간에 붙인다
            r_start = last
        out.append(segment[last:r_start])
        out.append(html_lib.escape(mask, quote=False) if decode_entities else mask)
        last = max(last, r_end)
    out.append(segment[last:])
    return "".join(out), len(spans)


def mask_html_document(document: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    """HTML 의 글과 주석만 가린다: 태그 · 속성(인라인 base64 그림 등) · 스크립트 · 스타일 · 문자 집합 표기는
    바이트 그대로다(파서로 다시 쓰면 문서 모양과 charset 표기가 바뀐다)."""
    if not document or not rules:
        return document, 0
    out: List[str] = []
    hits = 0
    for start, end, kind in _html_segments(document):
        piece = document[start:end]
        if kind != _MARKUP and piece:
            masked, count = _mask_html_text(piece, rules, decode_entities=kind == _TEXT)
            if count:
                piece, hits = masked, hits + count
        out.append(piece)
    return ("".join(out) if hits else document), hits


def _html_text(raw: bytes) -> str:
    document = _decode(raw).text
    parts = []
    for start, end, kind in _html_segments(document):
        if kind == _TEXT and end > start:
            parts.append(html_lib.unescape(document[start:end]))
        elif kind == _RAW and end > start:
            parts.append(document[start:end])
    return "\n".join(parts)


def _mask_html(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    decoded = _decode(raw)
    masked, hits = mask_html_document(decoded.text, rules)
    return (_encode(masked, decoded) if hits else raw), hits


# ── Office(OOXML) ───────────────────────────────────────────────────

#: 문자 데이터 태그. 접두사가 무엇이든(w:t · a:t · t · x:t), 삭제 추적 글(w:delText) · 필드 코드(w:instrText) ·
#: 스레드 메모(text) 까지. 글은 3번 묶음이다.
_OOXML_TEXT_RE = re.compile(
    r"(<((?:[A-Za-z][\w.-]*:)?(?:t|delText|instrText|text))(?:\s[^>]*)?>)([^<]*)(</\2>)"
)
#: 글 묶음(문단 · 공유 문자열 · 셀 안 문자열 · 옛 메모)의 여닫는 태그. 한 문장이 여러 run 으로 쪼개지므로 묶음 안은
#: 이어서 본다.
_OOXML_GROUP_TAG_RE = re.compile(
    r"<(/?)((?:w|a):p|(?:[A-Za-z][\w.-]*:)?(?:si|is|comment))(?=[\s/>])[^>]*?(/?)>"
)


def _unescape_xml(text: str) -> str:
    # 이름 있는 엔티티(&quot; &apos; …)와 숫자 참조(&#8220;)까지. saxutils.unescape 는 & < > 만 푼다.
    return html_lib.unescape(text)


def _ooxml_groups(xml: str) -> List[Tuple[int, int, bool]]:
    """``(시작, 끝, 이어서 볼 묶음인가)`` 구간. 묶음 밖 텍스트 노드는 노드마다 따로 본다(셀끼리 잇지 않는다).

    여닫는 태그를 한 번 훑어 깊이를 센다(닫히지 않은 태그가 많아도 다시 훑지 않는다).
    """
    groups: List[Tuple[int, int, bool]] = []
    last, depth = 0, 0
    for tag in _OOXML_GROUP_TAG_RE.finditer(xml):
        groups.append((last, tag.start(), depth > 0))
        if not tag.group(3):
            depth = max(0, depth - 1) if tag.group(1) else depth + 1
        groups.append((tag.start(), tag.end(), False))
        last = tag.end()
    groups.append((last, len(xml), depth > 0))
    return groups


def _mask_ooxml_fragment(fragment: str, rules: Sequence[MaskingRule], *, joined: bool) -> Tuple[str, int]:
    """조각 하나: 텍스트 노드를 (묶음이면 이어서) 찾고, 걸린 노드만 다시 쓴다.

    마스크는 매치가 시작하는 노드에 넣고 이어지는 노드에서는 그 부분을 지운다: 매치 밖 서식 · 엔티티는 그대로.
    """
    nodes = list(_OOXML_TEXT_RE.finditer(fragment))
    if not nodes:
        return fragment, 0
    pieces = [_unescape_xml(node.group(3)) for node in nodes]
    groups = [list(range(len(nodes)))] if joined else [[i] for i in range(len(nodes))]
    replaced: Dict[int, str] = {}
    hits = 0
    for group in groups:
        texts = [pieces[i] for i in group]
        spans = find_masked_spans("".join(texts), rules)
        if not spans:
            continue
        hits += len(spans)
        j, offset = 0, 0
        for i, piece in zip(group, texts):
            start, end = offset, offset + len(piece)
            offset = end
            while j < len(spans) and spans[j][1] <= start:
                j += 1
            out: List[str] = []
            cursor, touched, k = start, False, j
            while k < len(spans) and spans[k][0] < end:
                s_start, s_end, mask = spans[k]
                touched = True
                if cursor < s_start:
                    out.append(piece[cursor - start:s_start - start])
                if s_start >= start:
                    out.append(mask)
                cursor = max(cursor, min(s_end, end))
                if s_end > end:
                    break  # 다음 노드로 이어지는 매치
                k += 1
            if not touched:
                continue
            if cursor < end:
                out.append(piece[cursor - start:])
            replaced[i] = xml_escape("".join(out))
    if not replaced:
        return fragment, hits
    result: List[str] = []
    last = 0
    for index, node in enumerate(nodes):
        result.append(fragment[last:node.start(3)])
        result.append(replaced.get(index, node.group(3)))
        last = node.end(3)
    result.append(fragment[last:])
    return "".join(result), hits


def _mask_ooxml_part(xml: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    segments: List[str] = []
    hits = 0
    for start, end, joined in _ooxml_groups(xml):
        chunk = xml[start:end]
        masked, count = _mask_ooxml_fragment(chunk, rules, joined=joined)
        segments.append(masked)
        hits += count
    return "".join(segments), hits


def _xml_parts(source: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    # 그림 등 모든 항목을 다시 담으며 압축을 풀기 때문에 상한은 전체 항목에 건다(zipfile 은 적힌 크기를 지킨다).
    if sum(item.file_size for item in source.infolist()) > MASKING_MAX_UNZIPPED_BYTES:
        raise MaskingUnsupportedError("ooxml", f"압축을 푼 크기가 {MASKING_MAX_UNZIPPED_BYTES} 바이트 상한 초과")
    return [item for item in source.infolist() if item.filename.lower().endswith(".xml")]


def _mask_ooxml(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    """OOXML(zip) 안 XML 파트의 문자 데이터만 바꾼다. 파트는 UTF-8 고정(ECMA-376). 항목 순서 · 시각 · 압축 방식 유지."""
    hits = 0
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        xml_parts = {item.filename for item in _xml_parts(source)}
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename in xml_parts:
                    masked_xml, count = _mask_ooxml_part(data.decode("utf-8"), rules)
                    if count:
                        hits += count
                        data = masked_xml.encode("utf-8")
                target.writestr(item, data)
    return (output.getvalue() if hits else raw), hits


def _ooxml_text(raw: bytes) -> str:
    lines: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        for item in _xml_parts(source):
            xml = source.read(item.filename).decode("utf-8")
            for start, end, joined in _ooxml_groups(xml):
                pieces = [_unescape_xml(node.group(3)) for node in _OOXML_TEXT_RE.finditer(xml, start, end)]
                if not pieces:
                    continue
                if joined:
                    lines.append("".join(pieces))
                else:
                    lines.extend(pieces)
    return "\n".join(lines)


# ── PDF ──────────────────────────────────────────────────────────────

#: pdfium 은 스레드에 안전하지 않다: 같은 프로세스에서 두 일이 동시에 돌면 프로세스가 죽는다(재현됨). 이 모듈의
#: 일끼리만 막아서는 부족하다(같은 서비스의 문서 색인 · 미리보기도 pdfium 을 쓴다). 그래서 PDF 는 별도 작업
#: 프로세스(``_pdf_worker``)에서 돈다. 작업 프로세스 하나는 한 번에 한 일만 받는다.
_PDF_LOCK = threading.Lock()
_PDF_IDLE: List["_PdfWorker"] = []
_PDF_IDLE_LOCK = threading.Lock()
_PDF_SLOTS = threading.BoundedSemaphore(max(1, _PDF_WORKERS))
#: 작업 프로세스는 부모와 같은 import 경로로 이 SDK 만 불러온다(서비스의 시작 모듈은 불러오지 않는다).
_PDF_BOOT = (
    "import json, sys; sys.path[:] = json.loads(sys.argv[1]); "
    "from xgen_sdk.control_policy._pdf_worker import main; main()"
)
#: 파일 끝 문서 ID. pdfium 은 저장할 때마다 둘째 값을 무작위로 쓴다.
_PDF_ID_RE = re.compile(rb"/ID\s*\[\s*<([0-9A-Fa-f]*)>\s*<([0-9A-Fa-f]*)>\s*\]")


def _pdf_engine():
    try:
        import xgen_pdf
    except ImportError as exc:
        raise MaskingUnsupportedError("pdf", "PDF 엔진 없음") from exc
    return xgen_pdf


def _read_exact(stream, size: int) -> bytes:
    data = stream.read(size)
    if data is None or len(data) < size:
        raise EOFError
    return data


class _PdfWorker:
    """PDF 작업 프로세스 하나. 주고받기는 ``_pdf_worker`` 모듈 설명 참고."""

    def __init__(self) -> None:
        import json
        import subprocess
        import sys

        self.timed_out = False
        self.process = subprocess.Popen(
            [sys.executable, "-c", _PDF_BOOT, json.dumps(sys.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def alive(self) -> bool:
        return self.process.poll() is None

    def kill(self, timed_out: bool = False) -> None:
        self.timed_out = self.timed_out or timed_out
        try:
            self.process.kill()
            self.process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def call(self, name: str, args: tuple):
        payload = pickle.dumps((name, args))
        timer = threading.Timer(_PDF_TIMEOUT_S, self.kill, kwargs={"timed_out": True})
        timer.daemon = True
        timer.start()
        try:
            self.process.stdin.write(struct.pack(">Q", len(payload)) + payload)
            self.process.stdin.flush()
            size = struct.unpack(">Q", _read_exact(self.process.stdout, 8))[0]
            return pickle.loads(_read_exact(self.process.stdout, size))
        except (EOFError, OSError, pickle.UnpicklingError, struct.error) as exc:
            self.kill()
            if self.timed_out:
                raise MaskingUnsupportedError("pdf", f"{_PDF_TIMEOUT_S}초 안에 끝나지 않음") from exc
            raise MaskingUnsupportedError("pdf", "PDF 처리 중 작업 프로세스가 멈춤") from exc
        finally:
            timer.cancel()


def _run_pdf(job: str, *args):
    """PDF 일(이 모듈의 함수 이름)을 작업 프로세스에서. 시간이 넘거나 프로세스가 죽으면 가릴 수 없는 파일로 본다."""
    if _PDF_WORKERS <= 0:
        with _PDF_LOCK:
            return globals()[job](*args)
    with _PDF_SLOTS:
        with _PDF_IDLE_LOCK:
            worker = _PDF_IDLE.pop() if _PDF_IDLE else None
        if worker is None or not worker.alive():
            worker = _PdfWorker()
        status, value = worker.call(job, args)
        with _PDF_IDLE_LOCK:
            _PDF_IDLE.append(worker)
    if status == "ok":
        return value
    if status == "unsupported":
        raise MaskingUnsupportedError(*value)
    raise RuntimeError(f"PDF 작업 실패: {value}")


def _redaction_label(mask: str) -> str:
    # 내장 base14 글꼴로 못 그리는 마스크(한글 등)는 글자 없이 상자만 남긴다.
    return mask if mask.isascii() else ""


def _stable_pdf_id(content: bytes, seed: bytes) -> bytes:
    """문서 ID 를 입력에서 만든 같은 길이의 값으로 바꾼다(길이가 같아 xref 위치는 그대로다)."""

    def same_length(value: bytes, salt: bytes) -> bytes:
        digest = hashlib.sha256(seed + salt).hexdigest().upper().encode()
        return (digest * (len(value) // len(digest) + 1))[:len(value)]

    def fix(match: "re.Match[bytes]") -> bytes:
        whole = match.group(0)
        base = match.start(0)
        out = bytearray(whole)
        for group, salt in ((1, b"0"), (2, b"1")):
            s, e = match.start(group) - base, match.end(group) - base
            out[s:e] = same_length(whole[s:e], salt)
        return bytes(out)

    return _PDF_ID_RE.sub(fix, content)


def _mask_pdf_job(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    """(작업 프로세스 안) 페이지 글에서 걸린 구간을 찾아 좌표를 되짚은 뒤 redaction 으로 실제로 지운다."""
    pdf = _pdf_engine()
    hits = 0
    has_text_layer = False
    document = pdf.open(stream=raw, filetype="pdf")
    try:
        if document.needs_pass:
            raise MaskingUnsupportedError("pdf", "암호화된 PDF")
        for page in document:
            text = page.get_text("text") or ""
            if not text.strip():
                continue
            has_text_layer = True
            targets: List[Tuple[str, str]] = []
            # 글로 가린 결과와 같은 구간(규칙을 차례로, 앞 마스크 위에서 다시 찾는다)을 원문 위치로.
            for start, end, replacement in find_masked_spans(text, rules):
                matched = text[start:end]
                # 상자 위 글자는 그 자리의 치환문. 줄 수가 어긋나면(형식을 지키지 않는 부분 마스킹 등)
                # 글자 없이 상자만: 원문을 다시 그리지 않는다.
                replacement_lines = replacement.splitlines()
                for idx, line in enumerate(matched.splitlines()):
                    literal = line.strip()
                    # search_for 는 줄바꿈을 넘지 못해 줄마다 찾는다.
                    if not literal:
                        continue
                    label = replacement_lines[idx].strip() if idx < len(replacement_lines) else ""
                    if label == literal:
                        label = ""
                    if (literal, label) not in targets:
                        targets.append((literal, label))
            if not targets:
                continue
            redacted = False
            for literal, mask in targets:
                for rect in page.search_for(literal):
                    page.add_redact_annot(
                        rect,
                        text=_redaction_label(mask),
                        fontsize=max(6.0, min(11.0, rect.height * 0.8)),
                        cross_out=False,
                    )
                    hits += 1
                    redacted = True
            if redacted:
                page.apply_redactions()
        if not has_text_layer:
            raise MaskingUnsupportedError("pdf", "텍스트 레이어 없음(스캔 이미지)")
        if not hits:
            return raw, 0
        content = document.tobytes(garbage=3, deflate=True)
    finally:
        document.close()
    seed = hashlib.sha256(raw).digest() + "|".join(
        f"{r.policy_type}:{r.policy_id}:{r.pattern.pattern}:{r.mask}:{r.masking_mode}" for r in rules
    ).encode("utf-8", "surrogatepass")
    return _stable_pdf_id(content, seed), hits


def _pdf_text_job(raw: bytes) -> str:
    """(작업 프로세스 안) 기록용 페이지 글."""
    pdf = _pdf_engine()
    document = pdf.open(stream=raw, filetype="pdf")
    try:
        if document.needs_pass:
            return ""
        return "\n".join(page.get_text("text") or "" for page in document)
    finally:
        document.close()


def _mask_pdf(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    return _run_pdf("_mask_pdf_job", raw, list(rules))


def _pdf_text(raw: bytes) -> str:
    return _run_pdf("_pdf_text_job", raw)


# ── 형식 목록 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FormatMasker:
    name: str
    extensions: frozenset
    handler: Callable[[bytes, Sequence[MaskingRule]], Tuple[bytes, int]]
    #: 탐지 기록용 글(가리는 일은 handler 가 한다).
    text_of: Callable[[bytes], str]


FORMAT_MASKERS: Tuple[FormatMasker, ...] = (
    FormatMasker("pdf", frozenset({"pdf"}), _mask_pdf, _pdf_text),
    FormatMasker("html", frozenset({"html", "htm", "xhtml"}), _mask_html, _html_text),
    FormatMasker("ooxml", frozenset({"docx", "docm", "pptx", "pptm", "xlsx", "xlsm"}), _mask_ooxml, _ooxml_text),
    FormatMasker(
        "text",
        frozenset({
            "txt", "md", "markdown", "rtf", "log",
            "json", "yaml", "yml", "xml", "toml", "ini", "cfg", "conf", "properties", "env",
            "csv", "tsv",
            "py", "js", "ts", "jsx", "tsx", "java", "c", "cpp", "h", "hpp", "cs", "go",
            "rb", "php", "rs", "kt", "swift", "scala", "sql", "r", "m",
            "sh", "bat", "ps1", "zsh", "fish",
        }),
        _mask_plain_text,
        lambda raw: _decode(raw).text,
    ),
)

_MASKER_BY_EXTENSION: Dict[str, FormatMasker] = {
    extension: masker for masker in FORMAT_MASKERS for extension in masker.extensions
}


def masker_for_extension(extension: str) -> Optional[FormatMasker]:
    """이 확장자(점 없이)를 가릴 수단. 없으면 None."""
    return _MASKER_BY_EXTENSION.get((extension or "").lower().lstrip("."))


def masker_for(file_name: str) -> Optional[FormatMasker]:
    """이 파일 이름을 가릴 수단(확장자로 고른다). 확장자가 없거나 모르면 None."""
    extension = extension_of(file_name)
    return masker_for_extension(extension) if extension else None


def mask_bytes(raw: bytes, file_name: str, rules: Sequence[MaskingRule]) -> Tuple[bytes, int, str]:
    """``(바이트, 바꾼 건수, 형식 이름)``. 가릴 수단이 없으면 :class:`MaskingUnsupportedError`."""
    masker = masker_for(file_name)
    if masker is None:
        raise MaskingUnsupportedError(extension_of(file_name))
    if len(raw) > MASKING_MAX_BYTES:
        raise MaskingUnsupportedError(masker.name, f"{MASKING_MAX_BYTES} 바이트 상한 초과")
    content, hits = masker.handler(raw, rules)
    return content, hits, masker.name


def detection_text(raw: bytes, file_name: str) -> str:
    """기록용으로 이 파일의 글을 뽑는다(마스커가 보는 단위와 같다). 수단이 없으면 빈 글."""
    masker = masker_for(file_name)
    if masker is None:
        return ""
    return masker.text_of(raw)


# ── 업로드에서 지우기 ─────────────────────────────────────────────────

#: :class:`FileMasking.status`: ``masked`` 만 바이트가 바뀐다.
STATUS_OFF = "off"                # 쓸 규칙이 없다(자리가 꺼졌거나 정책이 없다)
STATUS_CLEAN = "clean"            # 읽었고 걸린 것이 없다
STATUS_MASKED = "masked"          # 걸린 부분을 지웠다
STATUS_UNMASKABLE = "unmaskable"  # 읽을 수단이 없어 그대로 두었다


@dataclass
class FileMasking:
    """파일 하나를 지운 결과.

    ``changed`` 일 때만 부르는 쪽이 ``content`` · ``size`` · ``sha256`` 으로 바꿔 저장한다(저장소 키 · 같은 내용
    판정 · 색인 검증이 모두 sha256 으로 돈다). 바뀌지 않았으면 원래 값을 그대로 쓴다: :func:`strip_file` 은
    읽지 않은 파일의 ``content`` 를 비워 둔다.
    """

    status: str
    content: Optional[bytes] = None
    hits: int = 0
    reason: str = ""
    format_name: str = ""
    #: 정책별 탐지(통제 정책 로그용, :meth:`MaskingResult.by_type`). 걸린 원문 샘플이 들어 있다.
    found: Optional[MaskingResult] = None

    @property
    def changed(self) -> bool:
        return self.status == STATUS_MASKED

    @property
    def size(self) -> Optional[int]:
        return None if self.content is None else len(self.content)

    @property
    def sha256(self) -> Optional[str]:
        return None if self.content is None else hashlib.sha256(self.content).hexdigest()

    def summary(self) -> Dict[str, object]:
        """응답 · 기록에 싣는 짧은 모양: 걸린 원문은 넣지 않는다."""
        out: Dict[str, object] = {"status": self.status, "hits": self.hits}
        if self.reason:
            out["reason"] = self.reason
        if self.found is not None:
            types = sorted({d.get("policy_type") or "" for d in self.found.detections} - {""})
            if types:
                out["policy_types"] = types
        return out


def strip_bytes(raw: bytes, file_name: str, rules: Sequence[MaskingRule]) -> FileMasking:
    """올라온 바이트에서 걸린 부분을 지운다. 지울 수 없으면 원본 그대로(``unmaskable``).

    CPU 를 쓰는 일(PDF redaction)이라 비동기 서버에서는 스레드에서 부른다.
    """
    if not rules:
        return FileMasking(STATUS_OFF, raw)
    try:
        content, hits, format_name = mask_bytes(raw, file_name, rules)
    except MaskingUnsupportedError as exc:
        return FileMasking(STATUS_UNMASKABLE, raw, reason=exc.reason, format_name=exc.extension)
    except Exception:  # noqa: BLE001, 깨진 파일 등: 고칠 수 없으면 그대로 둔다
        return FileMasking(STATUS_UNMASKABLE, raw, reason="파일을 읽지 못했습니다")
    if not hits or content == raw:
        return FileMasking(STATUS_CLEAN, raw, format_name=format_name)
    try:
        found = apply_rules(detection_text(raw, file_name), rules, mask=False)
    except Exception:  # noqa: BLE001, 기록용이라 실패해도 지운 결과는 쓴다
        found = None
    return FileMasking(STATUS_MASKED, content, hits=hits, format_name=format_name, found=found)


def strip_file(path: str, file_name: str, rules: Sequence[MaskingRule]) -> FileMasking:
    """``path`` 의 파일(부르는 쪽이 가진 임시 파일)을 지워 같은 자리에 다시 쓴다.

    지울 수 없거나 걸린 것이 없으면 파일을 건드리지 않는다. 상한을 넘는 파일은 읽지도 않는다. 다시 쓰기는
    옆 임시 파일에 쓴 뒤 바꿔 끼운다(쓰다 멈춰도 반쯤 쓴 파일이 남지 않는다).
    """
    if not rules:
        return FileMasking(STATUS_OFF)
    if masker_for(file_name) is None:
        return FileMasking(STATUS_UNMASKABLE, reason="지원하지 않는 형식", format_name=extension_of(file_name))
    if os.path.getsize(path) > MASKING_MAX_BYTES:
        return FileMasking(STATUS_UNMASKABLE, reason=f"{MASKING_MAX_BYTES} 바이트 상한 초과",
                           format_name=extension_of(file_name))
    with open(path, "rb") as stream:
        raw = stream.read()
    result = strip_bytes(raw, file_name, rules)
    if result.changed:
        directory = os.path.dirname(os.path.abspath(path))
        fd, staged = tempfile.mkstemp(prefix=".mask-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(result.content)
            os.replace(staged, path)
        finally:
            if os.path.exists(staged):
                os.unlink(staged)
    return result
