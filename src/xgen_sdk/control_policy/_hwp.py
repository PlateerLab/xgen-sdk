"""한글 문서(HWP 5.0 · HWPX)에서 통제 정책에 걸린 글을 지운다.

**HWP 5.0** 은 복합 문서(OLE) 안에 본문을 ``BodyText/SectionN`` 스트림으로 둔다(raw deflate 압축). 스트림은 레코드
(태그 10비트 · 깊이 10비트 · 크기 12비트) 의 줄이고, 문단은 ``PARA_HEADER`` 아래 ``PARA_TEXT``(UTF-16LE) 를 가진다.
xgen-doc2chunk 가 읽는 길과 같다. 글자 사이의 제어 문자는 한글 공개 문서(5.0, 표 6)를 따라 한 글자(줄바꿈 · 문단 끝
등) 또는 여덟 글자(탭 · 필드 · 표 · 그림 등) 크기다. 걸린 구간의 보통 글자만 마스크로 바꾸고 제어 문자는 하나도
지우지 않는다(표 · 그림 · 필드가 그대로 붙어 있다). 글자 수가 바뀌므로 문단 머리의 글자 수, 글자 모양 · 영역 태그의
위치를 함께 옮기고, 줄 배치 캐시(``PARA_LINE_SEG``)는 지운다(한컴이 다시 계산한다. 옛 캐시를 두면 새 글이 옛 줄
자리에 겹쳐 그려진다). 표 · 머리말 · 각주 · 글상자 안 글도 모두 문단이라 같은 길로 지운다.

미리보기 글(``PrvText``)도 가리고, 미리보기 그림(``PrvImage``)은 빈 그림으로 바꾼다(첫 쪽을 그린 그림이라 원문이
보인다). 문서 요약 정보(제목 · 작성자 등)의 글도 가린다. 문서 이력(``DocHistory``, 예전 판의 원문)은 지운다.
암호 · DRM · 배포용 문서는 본문이 암호화되어 있어 가릴 수 없다.

**HWPX** 는 zip 안 XML 이다(``Contents/sectionN.xml``). ``<hp:p>`` 문단마다 ``<hp:t>`` 글(사이에 ``<hp:tab/>`` 같은
요소가 섞일 수 있다)을 이어 보고, 바뀐 문단의 ``<hp:linesegarray>`` 를 지운다. 미리보기 · 메타데이터는 HWP 와 같다.
"""
from __future__ import annotations

import bisect
import html as html_lib
import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape

from . import _cfb
from .rules import MaskingRule, apply_rules, find_masked_spans

_PARA_HEADER, _PARA_TEXT, _PARA_CHAR_SHAPE, _PARA_LINE_SEG, _PARA_RANGE_TAG = 66, 67, 68, 69, 70
_FLAG_COMPRESSED, _FLAG_PASSWORD, _FLAG_DISTRIBUTE, _FLAG_DRM = 0x01, 0x02, 0x04, 0x10
_FLAG_HISTORY, _FLAG_CERT_ENCRYPTED, _FLAG_CERT_DRM = 0x40, 0x100, 0x400
_HWP_SIGNATURE = b"HWP Document File"
#: 여덟 글자 크기의 제어 문자(인라인 · 확장). 나머지 0~31 은 한 글자.
_WIDE_CONTROLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})
#: 찾을 때 글자로 보이는 제어 문자(doc2chunk 가 뽑는 글과 같게). 나머지는 폭 없이 건너뛴다.
_VISIBLE_CONTROLS = {9: "\t", 10: "\n", 13: "\n", 24: "-", 30: " ", 31: " "}
_SUMMARY_STREAM = "\x05HwpSummaryInformation"


def _unsupported(reason: str) -> Exception:
    from .files import MaskingUnsupportedError

    return MaskingUnsupportedError("hwp", reason)


def _safe_mask(text: str) -> str:
    # 마스크에 제어 문자가 있으면 문서 구조(문단 끝 · 필드)로 읽힌다: 빈칸으로.
    return "".join(" " if ord(ch) < 32 else ch for ch in text)


def _blank_png() -> bytes:
    """흰 점 하나짜리 PNG(미리보기 그림 자리)."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


# ── 문단 글: 단위로 쪼개고 다시 잇기 ─────────────────────────────────


@dataclass
class _Unit:
    control: bool
    raw: bytes   # UTF-16LE 바이트(제어 문자는 2 또는 16 바이트)
    text: str    # 찾을 때 보이는 글(제어 문자는 비거나 한 글자)


def _units(payload: bytes) -> List[_Unit]:
    if len(payload) % 2:
        raise _unsupported("문단 글 길이가 홀수(깨진 문서)")
    units: List[_Unit] = []
    pos, size = 0, len(payload)
    while pos < size:
        code = payload[pos] | (payload[pos + 1] << 8)
        if code < 32:
            width = 16 if code in _WIDE_CONTROLS else 2
            if pos + width > size:
                raise _unsupported("문단 글의 제어 문자가 잘림")
            units.append(_Unit(True, payload[pos:pos + width], _VISIBLE_CONTROLS.get(code, "")))
        else:
            width = 2
            if 0xD800 <= code <= 0xDBFF and pos + 4 <= size and 0xDC00 <= (payload[pos + 2] | (payload[pos + 3] << 8)) <= 0xDFFF:
                width = 4
            chunk = payload[pos:pos + width]
            units.append(_Unit(False, chunk, chunk.decode("utf-16-le", errors="surrogatepass")))
        pos += width
    return units


def _paragraph_text(units: List[_Unit]) -> Tuple[str, List[int]]:
    """찾을 글과 글자마다 단위 번호."""
    chars: List[str] = []
    owner: List[int] = []
    for index, unit in enumerate(units):
        for ch in unit.text:
            chars.append(ch)
            owner.append(index)
    return "".join(chars), owner


class _Rewrite:
    """한 문단의 바뀐 글과 옛 위치(WCHAR) → 새 위치.

    걸린 구간의 보통 글자는 마스크로 바뀌고, 구간 안의 제어 문자(필드 시작 · 끝, 탭 등)는 남아 마스크 사이 같은
    비율의 자리에 다시 놓인다(링크 · 누름틀이 덮는 범위가 옮겨 가지 않게). 구간 끝의 글자형 제어 문자(문단 끝)는
    마스크 뒤에 둔다.
    """

    def __init__(self, units: List[_Unit], owner: List[int], spans: List[Tuple[int, int, str]]):
        self.old_starts: List[int] = []
        chars_before: List[int] = []
        total = chars = 0
        for unit in units:
            self.old_starts.append(total)
            chars_before.append(chars)
            total += len(unit.raw) // 2
            chars += len(unit.text)
        self.old_total = total
        self.new_start: List[Optional[int]] = [None] * len(units)
        self.removed_at: Dict[int, int] = {}
        out: List[bytes] = []
        written = 0
        cursor = 0

        def emit(index: Optional[int], raw: bytes) -> None:
            nonlocal written
            if index is not None:
                self.new_start[index] = written
            out.append(raw)
            written += len(raw) // 2

        for start, end, mask in spans:
            first, last = owner[start], owner[end - 1]
            for k in range(cursor, first):
                emit(k, units[k].raw)
            replacement = _safe_mask(mask)
            length, size = end - start, len(replacement)
            placed: List[Tuple[int, int]] = []
            for k in range(first, last + 1):
                if not units[k].control:
                    continue
                offset = chars_before[k] - start
                if units[k].text:
                    at = size if offset >= length - 1 else (0 if offset <= 0 else round(offset * size / length))
                else:
                    at = 0 if offset <= 0 else (size if offset >= length else round(offset * size / length))
                placed.append((max(at, placed[-1][0]) if placed else at, k))
            replacement_at = written
            taken = 0
            for at, k in placed:
                if at > taken:
                    emit(None, replacement[taken:at].encode("utf-16-le", errors="surrogatepass"))
                    taken = at
                emit(k, units[k].raw)  # 표 · 필드 · 문단 끝은 남긴다
            if taken < size:
                emit(None, replacement[taken:].encode("utf-16-le", errors="surrogatepass"))
            for k in range(first, last + 1):
                if not units[k].control:
                    self.removed_at[k] = replacement_at
            cursor = last + 1
        for k in range(cursor, len(units)):
            emit(k, units[k].raw)
        self.payload = b"".join(out)
        self.new_total = written

    def position(self, old: int) -> int:
        if old >= self.old_total:
            return self.new_total + (old - self.old_total)
        k = bisect.bisect_right(self.old_starts, old) - 1
        if self.new_start[k] is None:
            return self.removed_at[k]
        return self.new_start[k] + (old - self.old_starts[k])


# ── HWP 5.0 레코드 ───────────────────────────────────────────────────


@dataclass
class _Record:
    tag: int
    level: int
    payload: bytes
    raw: Optional[bytes]  # 바뀌지 않았으면 원래 바이트 그대로(None 이면 다시 싼다)
    dropped: bool = False


def _records(data: bytes) -> List[_Record]:
    records: List[_Record] = []
    pos, size = 0, len(data)
    while pos < size:
        if pos + 4 > size:
            raise _unsupported("본문 레코드가 잘림")
        header = struct.unpack_from("<I", data, pos)[0]
        length, head = header >> 20, 4
        if length == 0xFFF:
            if pos + 8 > size:
                raise _unsupported("본문 레코드가 잘림")
            length, head = struct.unpack_from("<I", data, pos + 4)[0], 8
        end = pos + head + length
        if end > size:
            raise _unsupported("본문 레코드가 잘림")
        records.append(_Record(header & 0x3FF, (header >> 10) & 0x3FF, data[pos + head:end], data[pos:end]))
        pos = end
    return records


def _pack(record: _Record) -> bytes:
    if record.raw is not None:
        return record.raw
    size = len(record.payload)
    base = record.tag | (record.level << 10)
    if size >= 0xFFF:
        return struct.pack("<II", base | (0xFFF << 20), size) + record.payload
    return struct.pack("<I", base | (size << 20)) + record.payload


def _mask_paragraph(records: List[_Record], at: int, rules: Sequence[MaskingRule]) -> int:
    header = records[at]
    children: Dict[int, int] = {}
    j = at + 1
    while j < len(records) and records[j].level > header.level:
        if records[j].level == header.level + 1 and records[j].tag not in children:
            children[records[j].tag] = j
        j += 1
    text_at = children.get(_PARA_TEXT)
    if text_at is None or len(header.payload) < 18:
        return 0
    units = _units(records[text_at].payload)
    text, owner = _paragraph_text(units)
    spans = find_masked_spans(text, rules)
    if not spans:
        return 0
    rewrite = _Rewrite(units, owner, spans)
    records[text_at].payload, records[text_at].raw = rewrite.payload, None

    head = bytearray(header.payload)
    chars = struct.unpack_from("<I", head, 0)[0]
    struct.pack_into("<I", head, 0, (chars & 0x80000000) | rewrite.new_total)
    shape_at = children.get(_PARA_CHAR_SHAPE)
    if shape_at is not None:
        old = records[shape_at].payload
        shapes: List[Tuple[int, int]] = []
        for pos, shape in struct.iter_unpack("<II", old[:len(old) - len(old) % 8]):
            moved = rewrite.position(pos)
            if shapes and shapes[-1][0] == moved:
                shapes[-1] = (moved, shape)  # 같은 자리로 모이면 뒤의 모양이 그 자리부터다
            else:
                shapes.append((moved, shape))
        records[shape_at].payload = b"".join(struct.pack("<II", p, s) for p, s in shapes)
        records[shape_at].raw = None
        struct.pack_into("<H", head, 12, len(shapes))
    line_at = children.get(_PARA_LINE_SEG)
    if line_at is not None:
        records[line_at].dropped = True
    struct.pack_into("<H", head, 16, 0)  # 줄 배치는 한컴이 다시 계산한다
    range_at = children.get(_PARA_RANGE_TAG)
    if range_at is not None:
        old = records[range_at].payload
        tags = [(rewrite.position(s), rewrite.position(e), t)
                for s, e, t in struct.iter_unpack("<III", old[:len(old) - len(old) % 12])]
        records[range_at].payload = b"".join(struct.pack("<III", *t) for t in tags)
        records[range_at].raw = None
    header.payload, header.raw = bytes(head), None
    return len(spans)


def _mask_section(data: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    records = _records(data)
    hits = 0
    for index, record in enumerate(records):
        if record.tag == _PARA_HEADER:
            hits += _mask_paragraph(records, index, rules)
    if not hits:
        return data, 0
    return b"".join(_pack(r) for r in records if not r.dropped), hits


def _section_text(data: bytes) -> str:
    records = _records(data)
    lines: List[str] = []
    for index, record in enumerate(records):
        if record.tag != _PARA_HEADER:
            continue
        j = index + 1
        while j < len(records) and records[j].level > record.level:
            if records[j].level == record.level + 1 and records[j].tag == _PARA_TEXT:
                lines.append(_paragraph_text(_units(records[j].payload))[0].rstrip("\n"))
                break
            j += 1
    return "\n".join(lines)


# ── 문서 요약 정보(OLE 속성 집합) ────────────────────────────────────


def _mask_property_set(data: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    """속성 집합의 글 속성(VT_LPSTR · VT_LPWSTR)만 가리고 위치를 다시 맞춘다. 읽을 수 없으면 그대로."""
    try:
        if len(data) < 48 or struct.unpack_from("<H", data, 0)[0] != 0xFFFE:
            return data, 0
        count = struct.unpack_from("<I", data, 24)[0]
        if not 1 <= count <= 16:
            return data, 0
        table = [(data[28 + i * 20:44 + i * 20], struct.unpack_from("<I", data, 44 + i * 20)[0]) for i in range(count)]
        sections: List[bytes] = []
        hits = 0
        for _fmtid, offset in table:
            size = struct.unpack_from("<I", data, offset)[0]
            section = data[offset:offset + size]
            masked, found = _mask_property_section(section, rules)
            sections.append(masked)
            hits += found
        if not hits:
            return data, 0
        head = bytearray(data[:28 + count * 20])
        cursor = len(head)
        for i, section in enumerate(sections):
            struct.pack_into("<I", head, 44 + i * 20, cursor)
            cursor += len(section)
        return bytes(head) + b"".join(sections), hits
    except (struct.error, IndexError, UnicodeError, LookupError):
        return data, 0


def _mask_property_section(section: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    size, count = struct.unpack_from("<II", section, 0)
    entries = [struct.unpack_from("<II", section, 8 + i * 8) for i in range(count)]
    codepage = 1252
    for pid, offset in entries:
        if pid == 1 and struct.unpack_from("<I", section, offset)[0] == 2:  # VT_I2
            codepage = struct.unpack_from("<H", section, offset + 4)[0]
    encoding = "utf-16-le" if codepage == 1200 else ("utf-8" if codepage == 65001 else f"cp{codepage}")
    ordered = sorted(entries, key=lambda e: e[1])
    bounds = {offset: (ordered[i + 1][1] if i + 1 < len(ordered) else size) for i, (_, offset) in enumerate(ordered)}
    values: Dict[int, bytes] = {}
    hits = 0
    for pid, offset in entries:
        value = section[offset:bounds[offset]]
        kind = struct.unpack_from("<I", value, 0)[0] if len(value) >= 4 else 0
        if pid > 1 and kind in (0x1E, 0x1F) and len(value) >= 8:
            length = struct.unpack_from("<I", value, 4)[0]
            if kind == 0x1F:
                text = value[8:8 + length * 2].decode("utf-16-le").rstrip("\0")
            else:
                text = value[8:8 + length].decode(encoding).rstrip("\0")
            result = apply_rules(text, rules)
            if result.text != text:
                hits += result.total_detected
                if kind == 0x1F:
                    body = (result.text + "\0").encode("utf-16-le")
                    value = struct.pack("<II", kind, len(body) // 2) + body
                else:
                    body = (result.text + "\0").encode(encoding, errors="replace")
                    value = struct.pack("<II", kind, len(body)) + body
                value += b"\0" * (-len(value) % 4)
        values[offset] = value
    if not hits:
        return section, 0
    table = bytearray()
    body = bytearray()
    start = 8 + count * 8
    placed: Dict[int, int] = {}
    for _, offset in ordered:
        placed[offset] = start + len(body)
        body += values[offset]
    for pid, offset in entries:
        table += struct.pack("<II", pid, placed[offset])
    return struct.pack("<II", start + len(body), count) + bytes(table) + bytes(body), hits


# ── HWP 5.0 문서 ─────────────────────────────────────────────────────


def _open_hwp(raw: bytes) -> Tuple[_cfb.Entry, int, List[_cfb.Entry]]:
    if raw[:8] != _cfb.SIGNATURE:
        if _HWP_SIGNATURE in raw[:64]:
            raise _unsupported("한글 97 이전 형식")
        raise _unsupported("한글 문서가 아님")
    try:
        root = _cfb.read(raw)
    except _cfb.CfbError as exc:
        raise _unsupported(f"깨진 문서({exc})") from exc
    header = root.find("FileHeader")
    if header is None or not header.data.startswith(_HWP_SIGNATURE) or len(header.data) < 40:
        raise _unsupported("한글 문서가 아님")
    flags = struct.unpack_from("<I", header.data, 36)[0]
    if flags & (_FLAG_PASSWORD | _FLAG_DRM | _FLAG_CERT_ENCRYPTED | _FLAG_CERT_DRM):
        raise _unsupported("암호 · DRM 이 걸린 문서")
    if flags & _FLAG_DISTRIBUTE or root.child("ViewText") is not None:
        raise _unsupported("배포용 문서(본문 암호화)")
    body = root.child("BodyText")
    sections = [c for c in (body.children if body is not None else []) if c.is_stream and re.fullmatch(r"Section\d+", c.name)]
    if not sections:
        raise _unsupported("본문이 없음")
    sections.sort(key=lambda c: int(c.name[7:]))
    return root, flags, sections


def _section_data(entry: _cfb.Entry, compressed: bool) -> bytes:
    if not compressed:
        return entry.data
    try:
        return zlib.decompress(entry.data, -15)
    except zlib.error as exc:
        raise _unsupported("본문 압축을 풀지 못함") from exc


def _compress(data: bytes) -> bytes:
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    return packer.compress(data) + packer.flush()


def mask_hwp(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    if raw[:4] == b"PK\x03\x04":
        return mask_hwpx(raw, rules)  # 확장자는 .hwp 지만 HWPX 로 저장된 문서
    root, flags, sections = _open_hwp(raw)
    compressed = bool(flags & _FLAG_COMPRESSED)
    hits = 0
    for entry in sections:
        masked, count = _mask_section(_section_data(entry, compressed), rules)
        if count:
            entry.data = _compress(masked) if compressed else masked
            hits += count
    summary = root.child(_SUMMARY_STREAM)
    if summary is not None and summary.is_stream:
        summary.data, count = _mask_property_set(summary.data, rules)
        hits += count
    preview = root.child("PrvText")
    preview_changed = False
    if preview is not None and preview.is_stream and preview.data:
        text = preview.data.decode("utf-16-le", errors="surrogatepass")
        masked_text = apply_rules(text, rules).text
        if masked_text != text:
            preview.data = masked_text.encode("utf-16-le", errors="surrogatepass")
            preview_changed = True  # 본문의 앞부분을 옮긴 글이라 건수에 다시 세지 않는다
    if not hits and not preview_changed:
        return raw, 0
    image = root.child("PrvImage")
    if image is not None and image.is_stream:
        image.data = _blank_png()
    history = root.child("DocHistory")
    if history is not None:
        root.children.remove(history)  # 예전 판의 원문이 남아 있다
        header = root.find("FileHeader")
        data = bytearray(header.data)
        struct.pack_into("<I", data, 36, flags & ~_FLAG_HISTORY)
        header.data = bytes(data)
    return _cfb.write(root), hits


def hwp_text(raw: bytes) -> str:
    if raw[:4] == b"PK\x03\x04":
        return hwpx_text(raw)
    root, flags, sections = _open_hwp(raw)
    compressed = bool(flags & _FLAG_COMPRESSED)
    return "\n".join(_section_text(_section_data(entry, compressed)) for entry in sections)


# ── HWPX ────────────────────────────────────────────────────────────

_XML_TOKEN = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<(/?)([A-Za-z_][\w.:-]*)\b[^>]*?(/?)>", re.S)
_LEAF_TEXT = re.compile(r"(<([A-Za-z_][\w.:-]*)(?:\s[^>]*)?>)([^<]+)(</\2>)")


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def _hwpx_paragraphs(xml: str) -> List[dict]:
    """문단마다 자기 글 조각(``<hp:t>`` 안 글, 안쪽 문단 제외)과 줄 배치 캐시 자리."""
    paragraphs: List[dict] = []
    stack: List[int] = []
    in_text = False
    lineseg_open: Optional[Tuple[int, int]] = None
    last = 0
    for token in _XML_TOKEN.finditer(xml):
        if in_text and stack and token.start() > last:
            paragraphs[stack[-1]]["segments"].append((last, token.start()))
        last = token.end()
        name = token.group(2)
        if name is None:
            continue
        closing, self_closing, local = bool(token.group(1)), bool(token.group(3)), _local(name)
        if local == "p":
            if closing:
                if stack:
                    stack.pop()
            elif not self_closing:
                paragraphs.append({"segments": [], "lineseg": None})
                stack.append(len(paragraphs) - 1)
        elif local == "t":
            in_text = not closing and not self_closing
        elif local == "linesegarray" and stack:
            if self_closing:
                paragraphs[stack[-1]]["lineseg"] = (token.start(), token.end())
            elif not closing:
                lineseg_open = (stack[-1], token.start())
            elif lineseg_open is not None:
                paragraphs[lineseg_open[0]]["lineseg"] = (lineseg_open[1], token.end())
                lineseg_open = None
    return paragraphs


def _distribute(pieces: List[str], spans: List[Tuple[int, int, str]]) -> Dict[int, str]:
    """이어 본 조각들에 구간 마스크를 나눠 넣는다: 구간이 조각마다 덮는 비율대로 마스크를 자른다.

    조각 사이의 요소(``<hp:tab/>`` · 줄바꿈 · 필드 표시)가 마스크 안 같은 비율의 자리에 남는다.
    """
    replaced: Dict[int, str] = {}
    offset = 0
    first = 0
    for i, piece in enumerate(pieces):
        start, end = offset, offset + len(piece)
        offset = end
        while first < len(spans) and spans[first][1] <= start:
            first += 1
        out: List[str] = []
        cursor, k = start, first
        while k < len(spans) and spans[k][0] < end:
            s_start, s_end, mask = spans[k]
            lo, hi = max(s_start, start), min(s_end, end)
            if cursor < lo:
                out.append(piece[cursor - start:lo - start])
            length, size = s_end - s_start, len(mask)
            a = 0 if lo == s_start else round((lo - s_start) * size / length)
            b = size if hi == s_end else round((hi - s_start) * size / length)
            out.append(mask[a:b])
            cursor = hi
            if s_end > end:
                break
            k += 1
        if k > first or (k < len(spans) and spans[k][0] < end):
            if cursor < end:
                out.append(piece[cursor - start:])
            replaced[i] = "".join(out)
    return replaced


def _xml_text_mask(text: str) -> str:
    return xml_escape("".join(" " if ord(ch) < 32 and ch not in "\t\n" else ch for ch in text))


def _mask_hwpx_section(xml: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    edits: List[Tuple[int, int, str]] = []
    hits = 0
    for paragraph in _hwpx_paragraphs(xml):
        segments = paragraph["segments"]
        if not segments:
            continue
        pieces = [html_lib.unescape(xml[s:e]) for s, e in segments]
        spans = find_masked_spans("".join(pieces), rules)
        if not spans:
            continue
        hits += len(spans)
        for index, text in _distribute(pieces, spans).items():
            s, e = segments[index]
            edits.append((s, e, _xml_text_mask(text)))
        if paragraph["lineseg"] is not None:
            s, e = paragraph["lineseg"]
            edits.append((s, e, ""))  # 줄 배치 캐시: 한컴이 다시 계산한다
    if not edits:
        return xml, 0
    edits.sort()
    out: List[str] = []
    last = 0
    for s, e, value in edits:
        out.append(xml[last:s])
        out.append(value)
        last = e
    out.append(xml[last:])
    return "".join(out), hits


def _mask_leaf_text(xml: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    """메타데이터 XML 의 잎 요소 글(제목 · 작성자 등)을 요소마다 가린다."""
    hits = 0

    def fix(match: "re.Match[str]") -> str:
        nonlocal hits
        text = html_lib.unescape(match.group(3))
        result = apply_rules(text, rules)
        if result.text == text:
            return match.group(0)
        hits += result.total_detected
        return match.group(1) + _xml_text_mask(result.text) + match.group(4)

    masked = _LEAF_TEXT.sub(fix, xml)
    return masked, hits


def _hwpx_kind(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("contents/") and lowered.endswith("header.xml"):
        return "meta"  # 문서 정보(hh:docInfo): doc2chunk 가 메타데이터로 읽는다
    if lowered.startswith("contents/") and lowered.endswith(".xml"):
        return "section"
    if lowered.endswith(".hpf"):
        return "meta"
    if lowered == "preview/prvtext.txt":
        return "preview_text"
    if lowered.startswith("preview/prvimage"):
        return "preview_image"
    return ""


def _check_zip_size(source: zipfile.ZipFile) -> None:
    from .files import MASKING_MAX_UNZIPPED_BYTES

    if sum(item.file_size for item in source.infolist()) > MASKING_MAX_UNZIPPED_BYTES:
        raise _unsupported(f"압축을 푼 크기가 {MASKING_MAX_UNZIPPED_BYTES} 바이트 상한 초과")


def mask_hwpx(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    from .files import _mask_plain_text

    hits = 0
    replaced: Dict[str, bytes] = {}
    preview_changed = False
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        _check_zip_size(source)
        for item in source.infolist():
            kind = _hwpx_kind(item.filename)
            if kind in ("section", "meta"):
                xml = source.read(item.filename).decode("utf-8")
                masked, count = (_mask_hwpx_section if kind == "section" else _mask_leaf_text)(xml, rules)
                if count:
                    hits += count
                    replaced[item.filename] = masked.encode("utf-8")
            elif kind == "preview_text":
                data = source.read(item.filename)
                masked, count = _mask_plain_text(data, rules)
                if count:
                    preview_changed = True  # 본문 앞부분을 옮긴 글이라 건수에 다시 세지 않는다
                    replaced[item.filename] = masked
        if not hits and not preview_changed:
            return raw, 0
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                if _hwpx_kind(item.filename) == "preview_image":
                    data = _blank_png()  # 첫 쪽을 그린 그림이라 원문이 보인다
                else:
                    data = replaced.get(item.filename)
                    if data is None:
                        data = source.read(item.filename)
                target.writestr(item, data)  # 항목 순서 · 압축 방식(mimetype 은 무압축) 그대로
    return output.getvalue(), hits


def hwpx_text(raw: bytes) -> str:
    lines: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        _check_zip_size(source)
        for item in source.infolist():
            if _hwpx_kind(item.filename) != "section":
                continue
            xml = source.read(item.filename).decode("utf-8")
            for paragraph in _hwpx_paragraphs(xml):
                if paragraph["segments"]:
                    lines.append("".join(html_lib.unescape(xml[s:e]) for s, e in paragraph["segments"]))
    return "\n".join(lines)
