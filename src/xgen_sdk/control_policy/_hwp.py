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
            taken = 0  # 이미 쓴 마스크 글자 수
            # 구간 안 단위를 차례대로: 제어 문자는 같은 비율의 자리에 놓고, 지운 글자는 그 비율 자리를 새 위치로
            # 삼는다(글자 모양 · 영역 태그 위치가 뒤집히지 않게 차례를 지킨다).
            for k in range(first, last + 1):
                offset = chars_before[k] - start
                if units[k].control:
                    if units[k].text:
                        at = size if offset >= length - 1 else (0 if offset <= 0 else round(offset * size / length))
                    else:
                        at = 0 if offset <= 0 else (size if offset >= length else round(offset * size / length))
                    at = max(at, taken)
                    if at > taken:
                        emit(None, replacement[taken:at].encode("utf-16-le", errors="surrogatepass"))
                        taken = at
                    emit(k, units[k].raw)  # 표 · 필드 · 문단 끝은 남긴다
                else:
                    at = max(taken, min(size, round(offset * size / length)))
                    self.removed_at[k] = written + len(replacement[taken:at].encode("utf-16-le", errors="surrogatepass")) // 2
            if taken < size:
                emit(None, replacement[taken:].encode("utf-16-le", errors="surrogatepass"))
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
    if text_at is None or len(header.payload) < 4:
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
            moved = max(rewrite.position(pos), shapes[-1][0] if shapes else 0)  # 차례를 지킨다
            if shapes and shapes[-1][0] == moved:
                shapes[-1] = (moved, shape)  # 같은 자리로 모이면 뒤의 모양이 그 자리부터다
            else:
                shapes.append((moved, shape))
        records[shape_at].payload = b"".join(struct.pack("<II", p, s) for p, s in shapes)
        records[shape_at].raw = None
        if len(head) >= 14:  # 문단 머리는 판마다 길이가 다르다(16 · 22 · 24 바이트)
            struct.pack_into("<H", head, 12, len(shapes))
    line_at = children.get(_PARA_LINE_SEG)
    if line_at is not None:
        records[line_at].dropped = True
    if len(head) >= 18:
        struct.pack_into("<H", head, 16, 0)  # 줄 배치는 한컴이 다시 계산한다
    range_at = children.get(_PARA_RANGE_TAG)
    if range_at is not None:
        old = records[range_at].payload
        tags = []
        for s, e, t in struct.iter_unpack("<III", old[:len(old) - len(old) % 12]):
            start = rewrite.position(s)
            tags.append((start, max(start, rewrite.position(e)), t))
        records[range_at].payload = b"".join(struct.pack("<III", *t) for t in tags)
        records[range_at].raw = None
    header.payload, header.raw = bytes(head), None
    return len(spans)


#: 문단 글 밖에 사람이 쓴 글을 드는 자리(WSTR: UINT16 글자 수 + UTF-16LE). 한글 공개 문서와 python-hwpx 코덱이
#: 같은 위치를 쓴다. 필드(하이퍼링크 · 누름틀 등, 컨트롤 ID 가 % 로 시작)의 명령(링크 주소 · mailto), 개체(표 · 그리기 ·
#: 수식)의 설명(그림이면 원래 파일 이름), 수식 스크립트.
_FIELD_COMMAND_AT = 9
_OBJECT_DESCRIPTION_AT = 44
_OBJECT_CONTROLS = (b"tbl ", b"gso ", b"eqed")
_CTRL_HEADER, _EQEDIT = 71, 88


def _wstr_at(payload: bytes, offset: int) -> Optional[Tuple[int, str]]:
    if len(payload) < offset + 2:
        return None
    count = struct.unpack_from("<H", payload, offset)[0]
    if len(payload) < offset + 2 + count * 2:
        return None
    return count, payload[offset + 2:offset + 2 + count * 2].decode("utf-16-le", errors="surrogatepass")


def _string_slot(record: "_Record") -> Optional[int]:
    """이 레코드에서 사람이 쓴 글이 있는 자리(없으면 None)."""
    if record.tag == _CTRL_HEADER and len(record.payload) >= 4:
        ctrl = record.payload[:4][::-1]
        if ctrl.startswith(b"%"):
            return _FIELD_COMMAND_AT
        if ctrl in _OBJECT_CONTROLS and len(record.payload) >= _OBJECT_DESCRIPTION_AT + 2:
            return _OBJECT_DESCRIPTION_AT
    if record.tag == _EQEDIT:
        return 4
    return None


def _mask_string_slot(record: "_Record", rules: Sequence[MaskingRule]) -> int:
    offset = _string_slot(record)
    found = _wstr_at(record.payload, offset) if offset is not None else None
    if not found or not found[1]:
        return 0
    count, text = found
    result = apply_rules(text, rules)
    if result.text == text:
        return 0
    body = _safe_mask(result.text).encode("utf-16-le", errors="surrogatepass")
    if len(body) // 2 > 0xFFFF:
        raise _unsupported("개체 글이 너무 김")
    record.payload = (record.payload[:offset] + struct.pack("<H", len(body) // 2) + body
                      + record.payload[offset + 2 + count * 2:])
    record.raw = None
    return result.total_detected


def _mask_section(data: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    records = _records(data)
    hits = 0
    for index, record in enumerate(records):
        if record.tag == _PARA_HEADER:
            hits += _mask_paragraph(records, index, rules)
        else:
            hits += _mask_string_slot(record, rules)
    if not hits:
        return data, 0
    return b"".join(_pack(r) for r in records if not r.dropped), hits


def _section_text(data: bytes) -> str:
    records = _records(data)
    lines: List[str] = []
    for index, record in enumerate(records):
        if record.tag != _PARA_HEADER:
            offset = _string_slot(record)
            found = _wstr_at(record.payload, offset) if offset is not None else None
            if found and found[1]:
                lines.append(found[1])
            continue
        j = index + 1
        while j < len(records) and records[j].level > record.level:
            if records[j].level == record.level + 1 and records[j].tag == _PARA_TEXT:
                lines.append(_paragraph_text(_units(records[j].payload))[0].rstrip("\n"))
                break
            j += 1
    return "\n".join(lines)


def _body_text(data: bytes) -> str:
    """문단 글만(미리보기 글을 다시 만들 때)."""
    records = _records(data)
    lines: List[str] = []
    for index, record in enumerate(records):
        if record.tag == _PARA_HEADER:
            j = index + 1
            while j < len(records) and records[j].level > record.level:
                if records[j].level == record.level + 1 and records[j].tag == _PARA_TEXT:
                    lines.append(_paragraph_text(_units(records[j].payload))[0].rstrip("\n"))
                    break
                j += 1
    return "\n".join(lines)


# ── 문서 요약 정보(OLE 속성 집합) ────────────────────────────────────


class _Unreadable(Exception):
    """구조를 읽지 못했다(부르는 쪽이 다른 길을 고른다)."""


def _property_sections(data: bytes) -> List[Tuple[int, bytes]]:
    if len(data) < 48 or struct.unpack_from("<H", data, 0)[0] != 0xFFFE:
        raise _Unreadable()
    count = struct.unpack_from("<I", data, 24)[0]
    if not 1 <= count <= 16 or len(data) < 28 + count * 20:
        raise _Unreadable()
    out = []
    for i in range(count):
        offset = struct.unpack_from("<I", data, 44 + i * 20)[0]
        size = struct.unpack_from("<I", data, offset)[0]
        if offset + size > len(data) or size < 8:
            raise _Unreadable()
        out.append((offset, data[offset:offset + size]))
    return out


def _mask_property_set(data: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    """속성 집합의 글 속성(VT_LPSTR · VT_LPWSTR)만 가리고 위치를 다시 맞춘다. 읽지 못하면 :class:`_Unreadable`."""
    try:
        parts = _property_sections(data)
        sections: List[bytes] = []
        hits = 0
        for _offset, section in parts:
            masked, found = _mask_property_section(section, rules)
            sections.append(masked)
            hits += found
    except (struct.error, IndexError, UnicodeError, LookupError, KeyError) as exc:
        raise _Unreadable() from exc
    if not hits:
        return data, 0
    count = len(parts)
    head = bytearray(data[:28 + count * 20])
    cursor = len(head)
    for i, section in enumerate(sections):
        struct.pack_into("<I", head, 44 + i * 20, cursor)
        cursor += len(section)
    return bytes(head) + b"".join(sections), hits


def _property_texts(data: bytes) -> List[str]:
    try:
        texts: List[str] = []
        for _offset, section in _property_sections(data):
            _mask_property_section(section, [], collect=texts)
        return texts
    except (_Unreadable, struct.error, IndexError, UnicodeError, LookupError, KeyError):
        return []


def _loose_hits(data: bytes, rules: Sequence[MaskingRule]) -> int:
    """구조를 모르는 바이트에서 글로 보이는 것을 찾는다(UTF-16 두 정렬 · cp949). 지울지 판단할 때만 쓴다."""
    found = 0
    for text in (data.decode("utf-16-le", errors="ignore"), data[1:].decode("utf-16-le", errors="ignore"),
                 data.decode("cp949", errors="ignore")):
        found = max(found, apply_rules(text, rules, mask=False).total_detected)
    return found


def _mask_property_section(section: bytes, rules: Sequence[MaskingRule], *,
                           collect: Optional[List[str]] = None) -> Tuple[bytes, int]:
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
            if collect is not None:
                collect.append(text)
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


def _budget() -> List[int]:
    from .files import MASKING_MAX_UNZIPPED_BYTES

    return [MASKING_MAX_UNZIPPED_BYTES]


def _inflate(data: bytes, budget: List[int]) -> bytes:
    """raw deflate 를 푼다. 한 문서에서 푼 크기의 합이 상한을 넘으면 멈춘다(작은 파일이 수십 GB 로 풀리는 문서)."""
    inflater = zlib.decompressobj(-15)
    try:
        out = inflater.decompress(data, budget[0] + 1)
    except zlib.error as exc:
        raise _unsupported("압축을 풀지 못함") from exc
    if len(out) > budget[0] or inflater.unconsumed_tail:
        raise _unsupported("압축을 푼 크기가 상한 초과")
    budget[0] -= len(out)
    return out


def _section_data(entry: _cfb.Entry, compressed: bool, budget: List[int]) -> bytes:
    return _inflate(entry.data, budget) if compressed else entry.data


def _compress(data: bytes) -> bytes:
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    return packer.compress(data) + packer.flush()


def _history_hits(history: _cfb.Entry, rules: Sequence[MaskingRule], budget: List[int]) -> int:
    """문서 이력(예전 판들)에 걸린 것이 있는가. 구조를 읽지 않고 스트림마다 풀어(되면) 글로 보이는 것을 찾는다."""
    found = 0
    for _path, entry in history.streams():
        data = entry.data
        try:
            data = _inflate(entry.data, budget)
        except Exception:  # noqa: BLE001, 압축이 아니면 그대로 본다(상한 초과도 그대로 보고 지운다)
            pass
        found += _loose_hits(data, rules)
    return found


def mask_hwp(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    """걸린 건수는 본문(문단 글 · 필드 명령 · 개체 설명 · 수식)과 문서 요약 정보만 센다. 미리보기 · 이력은 본문을
    옮긴 것이라 본문에 걸린 것이 없을 때만 센다(그래야 그것만 걸린 문서도 지운 문서가 된다)."""
    if raw[:4] == b"PK\x03\x04":
        return mask_hwpx(raw, rules)  # 확장자는 .hwp 지만 HWPX 로 저장된 문서
    root, flags, sections = _open_hwp(raw)
    compressed = bool(flags & _FLAG_COMPRESSED)
    budget = _budget()
    hits = 0
    body_lines: List[str] = []
    for entry in sections:
        masked, count = _mask_section(_section_data(entry, compressed, budget), rules)
        if count:
            entry.data = _compress(masked) if compressed else masked
            hits += count
        body_lines.append(_body_text(masked))
    summary = root.child(_SUMMARY_STREAM)
    if summary is not None and summary.is_stream:
        try:
            summary.data, count = _mask_property_set(summary.data, rules)
        except _Unreadable:
            count = _loose_hits(summary.data, rules)
            if count:
                root.children.remove(summary)  # 읽지 못하는 요약 정보에 걸린 것이 있으면 통째로 뺀다
        hits += count
    extra = 0
    preview = root.child("PrvText")
    if preview is not None and preview.is_stream and preview.data:
        text = preview.data.decode("utf-16-le", errors="surrogatepass")
        # 미리보기는 본문 앞부분을 잘라 옮긴 글이다. 매치 한가운데서 잘렸으면 규칙이 다시 못 찾으므로, 본문을 고쳤으면
        # 고친 본문에서 같은 길이로 다시 만든다.
        rebuilt = "\r\n".join(body_lines).replace("\n", "\r\n").replace("\r\r\n", "\r\n")[:len(text)] if hits else None
        found = apply_rules(text, rules)
        new_text = rebuilt if rebuilt is not None else found.text
        if new_text != text:
            preview.data = new_text.encode("utf-16-le", errors="surrogatepass")
            extra += found.total_detected
    history = root.child("DocHistory")
    history_found = _history_hits(history, rules, budget) if history is not None else 0
    extra += history_found
    if not hits and not extra:
        return raw, 0
    image = root.child("PrvImage")
    if image is not None and image.is_stream:
        image.data = _blank_png()  # 첫 쪽을 그린 그림이라 원문이 보인다
    if history is not None:
        root.children.remove(history)  # 예전 판의 원문이 남아 있다
        header = root.find("FileHeader")
        data = bytearray(header.data)
        struct.pack_into("<I", data, 36, flags & ~_FLAG_HISTORY)
        header.data = bytes(data)
    return _cfb.write(root), (hits or max(1, extra))


def hwp_text(raw: bytes) -> str:
    if raw[:4] == b"PK\x03\x04":
        return hwpx_text(raw)
    root, flags, sections = _open_hwp(raw)
    compressed = bool(flags & _FLAG_COMPRESSED)
    budget = _budget()
    parts = [_section_text(_section_data(entry, compressed, budget)) for entry in sections]
    summary = root.child(_SUMMARY_STREAM)
    if summary is not None and summary.is_stream:
        parts.extend(_property_texts(summary.data))
    return "\n".join(p for p in parts if p)


# ── HWPX ────────────────────────────────────────────────────────────

#: 속성 값 안의 > 도 태그 끝으로 보지 않는다.
_ATTRS = r"""(?:"[^"]*"|'[^']*'|[^'">])*?"""
_XML_TOKEN = re.compile(r"<!--.*?-->|<!\[CDATA\[(.*?)\]\]>|<\?.*?\?>|<(/?)([A-Za-z_][\w.:-]*)\b" + _ATTRS + r"(/?)>", re.S)
_LEAF_TEXT = re.compile(r"(<([A-Za-z_][\w.:-]*)(?:\s" + _ATTRS + r")?>)([^<]+)(</\2>)")
#: 문단 글 밖에 사람이 쓴 글을 드는 HWPX 요소: 필드 매개변수(하이퍼링크 주소 · mailto 등), 개체 설명, 수식 스크립트.
_HWPX_STRING_LEAVES = frozenset({"stringParam", "shapeComment", "script"})
#: content.hpf 에서 사람이 쓴 글인 메타데이터(날짜 등 정해진 꼴의 값은 건드리지 않는다).
_HPF_TEXT_META = frozenset({"creator", "subject", "description", "lastsaveby", "keyword", "title"})


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def _hwpx_paragraphs(xml: str) -> List[dict]:
    """문단마다 자기 글 조각(``<hp:t>`` 안 글, 안쪽 문단 제외)과 줄 배치 캐시 자리.

    조각은 ``(시작, 끝, CDATA 인가)``. ``<hp:t>`` 안 CDATA 의 글도 조각이다.
    """
    paragraphs: List[dict] = []
    stack: List[int] = []
    in_text = False
    lineseg_open: Optional[Tuple[int, int]] = None
    last = 0
    for token in _XML_TOKEN.finditer(xml):
        if in_text and stack and token.start() > last:
            paragraphs[stack[-1]]["segments"].append((last, token.start(), False))
        last = token.end()
        name = token.group(3)
        if name is None:
            if in_text and stack and token.group(1) is not None and token.group(1):
                paragraphs[stack[-1]]["segments"].append((token.start(1), token.end(1), True))
            continue
        closing, self_closing, local = bool(token.group(2)), bool(token.group(4)), _local(name)
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


def _segment_text(xml: str, segment: Tuple[int, int, bool]) -> str:
    start, end, cdata = segment
    return xml[start:end] if cdata else html_lib.unescape(xml[start:end])


def _segment_value(text: str, cdata: bool) -> str:
    if cdata:
        return "".join(" " if ord(ch) < 32 and ch not in "\t\n" else ch for ch in text).replace("]]>", "]] >")
    return _xml_text_mask(text)


def _string_leaves(xml: str) -> List["re.Match[str]"]:
    return [m for m in _LEAF_TEXT.finditer(xml) if _local(m.group(2)) in _HWPX_STRING_LEAVES]


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
        pieces = [_segment_text(xml, segment) for segment in segments]
        spans = find_masked_spans("".join(pieces), rules)
        if not spans:
            continue
        hits += len(spans)
        for index, text in _distribute(pieces, spans).items():
            s, e, cdata = segments[index]
            edits.append((s, e, _segment_value(text, cdata)))
        if paragraph["lineseg"] is not None:
            s, e = paragraph["lineseg"]
            edits.append((s, e, ""))  # 줄 배치 캐시: 한컴이 다시 계산한다
    for leaf in _string_leaves(xml):
        text = html_lib.unescape(leaf.group(3))
        result = apply_rules(text, rules)
        if result.text != text:
            hits += result.total_detected
            edits.append((leaf.start(3), leaf.end(3), _xml_text_mask(result.text)))
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


def _mask_leaf_text(xml: str, rules: Sequence[MaskingRule], keep=None) -> Tuple[str, int]:
    """메타데이터 XML 의 잎 요소 글(제목 · 작성자 등)을 요소마다 가린다. ``keep(match)`` 가 거짓인 요소는 건너뛴다."""
    hits = 0

    def fix(match: "re.Match[str]") -> str:
        nonlocal hits
        if keep is not None and not keep(match):
            return match.group(0)
        text = html_lib.unescape(match.group(3))
        result = apply_rules(text, rules)
        if result.text == text:
            return match.group(0)
        hits += result.total_detected
        return match.group(1) + _xml_text_mask(result.text) + match.group(4)

    masked = _LEAF_TEXT.sub(fix, xml)
    return masked, hits


_NAME_ATTR = re.compile(r"""\bname\s*=\s*["']([^"']*)["']""")
_DOC_INFO = re.compile(r"(<((?:[A-Za-z_][\w.-]*:)?docInfo)\b" + _ATTRS + r">)(.*?)(</\2>)", re.S)


def _hpf_text_meta(match: "re.Match[str]") -> bool:
    local = _local(match.group(2))
    if local == "title":
        return True
    name = _NAME_ATTR.search(match.group(1))
    return local == "meta" and name is not None and name.group(1).lower() in _HPF_TEXT_META


def _mask_hpf(xml: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    return _mask_leaf_text(xml, rules, keep=_hpf_text_meta)


def _mask_header(xml: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    """header.xml 은 문서 정보(hh:docInfo) 안 글만(글꼴 · 스타일 이름은 그대로)."""
    hits = 0

    def fix(match: "re.Match[str]") -> str:
        nonlocal hits
        inner, count = _mask_leaf_text(match.group(3), rules)
        hits += count
        return match.group(1) + inner + match.group(4)

    return _DOC_INFO.sub(fix, xml), hits


def _hwpx_kind(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("contents/") and lowered.endswith("header.xml"):
        return "header"  # 문서 정보(hh:docInfo): doc2chunk 가 메타데이터로 읽는다
    if lowered.startswith("contents/") and lowered.endswith(".xml"):
        return "section"
    if lowered.endswith(".hpf"):
        return "meta"
    if lowered == "preview/prvtext.txt":
        return "preview_text"
    if lowered.startswith("preview/prvimage"):
        return "preview_image"
    return ""


def _check_zip(source: zipfile.ZipFile) -> None:
    from .files import MASKING_MAX_UNZIPPED_BYTES

    names = [item.filename for item in source.infolist()]
    if len(names) != len(set(names)):
        raise _unsupported("zip 항목 이름이 겹침")  # 읽는 쪽마다 다른 항목을 볼 수 있다
    if sum(item.file_size for item in source.infolist()) > MASKING_MAX_UNZIPPED_BYTES:
        raise _unsupported(f"압축을 푼 크기가 {MASKING_MAX_UNZIPPED_BYTES} 바이트 상한 초과")


def _section_lines(xml: str) -> List[str]:
    lines = ["".join(_segment_text(xml, s) for s in p["segments"]) for p in _hwpx_paragraphs(xml) if p["segments"]]
    lines.extend(html_lib.unescape(leaf.group(3)) for leaf in _string_leaves(xml))
    return lines


def mask_hwpx(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    """건수는 본문 · 메타데이터만 센다(미리보기는 본문에 걸린 것이 없을 때만)."""
    from .files import _decode, _encode

    maskers = {"section": _mask_hwpx_section, "meta": _mask_hpf, "header": _mask_header}
    hits = 0
    replaced: Dict[str, bytes] = {}
    body_lines: List[str] = []
    preview_item: Optional[zipfile.ZipInfo] = None
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        _check_zip(source)
        for item in source.infolist():
            kind = _hwpx_kind(item.filename)
            if kind in maskers:
                xml = source.read(item.filename).decode("utf-8")
                masked, count = maskers[kind](xml, rules)
                if count:
                    hits += count
                    replaced[item.filename] = masked.encode("utf-8")
                if kind == "section":
                    body_lines.extend(
                        "".join(_segment_text(masked, s) for s in p["segments"])
                        for p in _hwpx_paragraphs(masked) if p["segments"])
            elif kind == "preview_text":
                preview_item = item
        extra = 0
        if preview_item is not None:
            data = source.read(preview_item.filename)
            decoded = _decode(data)
            found = apply_rules(decoded.text, rules)
            # 미리보기는 본문 앞부분을 잘라 옮긴 글이다: 본문을 고쳤으면 고친 본문에서 같은 길이로 다시 만든다.
            new_text = "\r\n".join(body_lines)[:len(decoded.text)] if hits else found.text
            if new_text != decoded.text:
                replaced[preview_item.filename] = _encode(new_text, decoded)
                extra = found.total_detected
        if not hits and not extra:
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
    return output.getvalue(), (hits or max(1, extra))


def hwpx_text(raw: bytes) -> str:
    lines: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        _check_zip(source)
        for item in source.infolist():
            kind = _hwpx_kind(item.filename)
            if kind == "section":
                lines.extend(_section_lines(source.read(item.filename).decode("utf-8")))
            elif kind == "meta":
                xml = source.read(item.filename).decode("utf-8")
                lines.extend(html_lib.unescape(m.group(3)) for m in _LEAF_TEXT.finditer(xml) if _hpf_text_meta(m))
    return "\n".join(lines)
