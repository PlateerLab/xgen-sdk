"""복합 문서(Compound File Binary, OLE2) 읽기 · 다시 쓰기: HWP 5.0 문서의 그릇.

[MS-CFB] 를 따른다. 읽기는 512 · 4096 바이트 섹터 모두, 쓰기는 버전 3(512 바이트 섹터)으로 한다. 항목의 이름 ·
종류 · CLSID · 상태 비트 · 시각은 그대로 옮기고, 스트림 내용만 부르는 쪽이 바꾼다. 형제 항목은 이름 차례
(길이 먼저, 그다음 대문자 비교)로 균형 잡힌 레드-블랙 트리로 다시 묶는다.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_FATSECT = 0xFFFFFFFD
_DIFSECT = 0xFFFFFFFC
_MAXREGSECT = 0xFFFFFFFA
_NOSTREAM = 0xFFFFFFFF
_STORAGE, _STREAM, _ROOT = 1, 2, 5
_RED, _BLACK = 0, 1
_SECTOR = 512
_MINI_SECTOR = 64
_MINI_CUTOFF = 4096
_HEADER_DIFAT = 109


class CfbError(ValueError):
    """복합 문서가 아니거나 깨졌다."""


@dataclass
class Entry:
    """저장소(``children``) 또는 스트림(``data``) 하나."""

    name: str
    kind: int
    data: bytes = b""
    clsid: bytes = b"\0" * 16
    state: int = 0
    created: bytes = b"\0" * 8
    modified: bytes = b"\0" * 8
    children: List["Entry"] = field(default_factory=list)

    @property
    def is_stream(self) -> bool:
        return self.kind == _STREAM

    def child(self, name: str) -> Optional["Entry"]:
        return next((c for c in self.children if c.name == name), None)

    def find(self, path: str) -> Optional["Entry"]:
        """``"BodyText/Section0"`` 같은 경로의 항목."""
        node: Optional[Entry] = self
        for part in path.split("/"):
            node = node.child(part) if node is not None else None
        return node

    def streams(self, prefix: str = "") -> List[tuple]:
        """``(경로, 항목)`` 을 깊이 우선으로."""
        out = []
        for c in self.children:
            path = f"{prefix}{c.name}"
            if c.is_stream:
                out.append((path, c))
            else:
                out.extend(c.streams(path + "/"))
        return out


def _u32(raw: bytes, offset: int) -> int:
    return struct.unpack_from("<I", raw, offset)[0]


def read(raw: bytes) -> Entry:
    """바이트 → 루트 항목. 깨졌으면 :class:`CfbError`."""
    if len(raw) < 512 or raw[:8] != SIGNATURE:
        raise CfbError("복합 문서가 아님")
    _minor, major, byte_order, sector_shift, mini_shift = struct.unpack_from("<HHHHH", raw, 0x18)
    if byte_order != 0xFFFE or sector_shift not in (9, 12) or mini_shift != 6:
        raise CfbError("복합 문서 머리가 이상함")
    sector = 1 << sector_shift
    count = (len(raw) - sector) // sector  # 머리(섹터 하나) 뒤의 섹터 수

    def sector_bytes(index: int) -> bytes:
        if index >= count:
            raise CfbError("섹터 번호가 파일 밖")
        start = (index + 1) * sector
        return raw[start:start + sector]

    per_sector = sector // 4
    fat_sectors = [s for s in struct.unpack_from(f"<{_HEADER_DIFAT}I", raw, 0x4C) if s <= _MAXREGSECT]
    difat, seen = _u32(raw, 0x44), set()
    while difat <= _MAXREGSECT:
        if difat in seen:
            raise CfbError("DIFAT 고리")
        seen.add(difat)
        values = struct.unpack(f"<{per_sector}I", sector_bytes(difat))
        fat_sectors.extend(s for s in values[:-1] if s <= _MAXREGSECT)
        difat = values[-1]
    fat: List[int] = []
    for s in fat_sectors:
        fat.extend(struct.unpack(f"<{per_sector}I", sector_bytes(s)))

    def chain(start: int, table: List[int]) -> List[int]:
        out, visited = [], set()
        while start <= _MAXREGSECT:
            if start in visited or start >= len(table):
                raise CfbError("섹터 사슬이 깨짐")
            visited.add(start)
            out.append(start)
            start = table[start]
        return out

    def regular(start: int) -> bytes:
        return b"".join(sector_bytes(s) for s in chain(start, fat))

    directory = regular(_u32(raw, 0x30))
    records = [directory[i:i + 128] for i in range(0, len(directory) - 127, 128)]
    if not records:
        raise CfbError("디렉터리가 없음")

    def size_of(record: bytes) -> int:
        low, high = struct.unpack_from("<II", record, 0x78)
        return low if major == 3 else (high << 32) | low

    mini_cutoff = _u32(raw, 0x38)
    root_record = records[0]
    mini_stream = regular(_u32(root_record, 0x74))[:size_of(root_record)] if size_of(root_record) else b""
    minifat: List[int] = []
    if _u32(raw, 0x40):
        mf = regular(_u32(raw, 0x3C))
        minifat = list(struct.unpack(f"<{len(mf) // 4}I", mf))

    def stream_data(record: bytes) -> bytes:
        size, start = size_of(record), _u32(record, 0x74)
        if size == 0:
            return b""
        if size < mini_cutoff:
            data = b"".join(mini_stream[s * _MINI_SECTOR:(s + 1) * _MINI_SECTOR] for s in chain(start, minifat))
        else:
            data = regular(start)
        if len(data) < size:
            raise CfbError("스트림이 잘림")
        return data[:size]

    def entry_of(sid: int, depth: int) -> Entry:
        record = records[sid]
        name_len = struct.unpack_from("<H", record, 0x40)[0]
        name = record[:max(0, min(64, name_len) - 2)].decode("utf-16-le", errors="replace")
        kind = record[0x42]
        node = Entry(
            name=name, kind=kind, clsid=record[0x50:0x60], state=_u32(record, 0x60),
            created=record[0x64:0x6C], modified=record[0x6C:0x74],
            data=stream_data(record) if kind == _STREAM else b"",
        )
        if kind in (_STORAGE, _ROOT):
            node.children = [entry_of(c, depth + 1) for c in siblings(_u32(record, 0x4C), depth)]
        return node

    def siblings(sid: int, depth: int) -> List[int]:
        if depth > 64:
            raise CfbError("저장소가 너무 깊음")
        out, stack, visited = [], [sid], set()
        while stack:
            current = stack.pop()
            if current == _NOSTREAM:
                continue
            if current in visited or current >= len(records):
                raise CfbError("디렉터리 트리가 깨짐")
            visited.add(current)
            out.append(current)
            stack.append(_u32(records[current], 0x44))
            stack.append(_u32(records[current], 0x48))
        return out

    if root_record[0x42] != _ROOT:
        raise CfbError("루트 항목이 없음")
    return entry_of(0, 0)


def _key(name: str) -> tuple:
    # [MS-CFB] 2.6.4: 짧은 이름이 먼저, 길이가 같으면 대문자로 바꿔 UTF-16 단위로 비교.
    return (len(name.encode("utf-16-le")), name.upper())


def write(root: Entry) -> bytes:
    """루트 항목 → 버전 3 복합 문서 바이트."""
    nodes: List[Entry] = []
    sid_of: Dict[int, int] = {}
    links: Dict[int, List[int]] = {}  # sid → [왼쪽, 오른쪽, 자식, 색]

    def number(node: Entry) -> None:
        sid_of[id(node)] = len(nodes)
        nodes.append(node)
        for child in node.children:
            number(child)

    number(root)
    for node in nodes:
        links[sid_of[id(node)]] = [_NOSTREAM, _NOSTREAM, _NOSTREAM, _BLACK]
    for node in nodes:
        if node.kind in (_STORAGE, _ROOT) and node.children:
            ordered = sorted(node.children, key=lambda c: _key(c.name))
            links[sid_of[id(node)]][2] = _tree([sid_of[id(c)] for c in ordered], links)

    # 스트림 자리: 4096 바이트 미만은 미니 스트림(64 바이트 섹터)에, 나머지는 일반 섹터에.
    mini = bytearray()
    minifat: List[int] = []
    start_of: Dict[int, int] = {}
    big: List[Entry] = []
    for node in nodes:
        if node.kind != _STREAM:
            continue
        size = len(node.data)
        if size == 0:
            start_of[id(node)] = _ENDOFCHAIN
        elif size < _MINI_CUTOFF:
            first = len(mini) // _MINI_SECTOR
            sectors = -(-size // _MINI_SECTOR)
            mini += node.data + b"\0" * (sectors * _MINI_SECTOR - size)
            minifat.extend(range(first + 1, first + sectors))
            minifat.append(_ENDOFCHAIN)
            start_of[id(node)] = first
        else:
            big.append(node)

    def sectors_for(size: int) -> int:
        return -(-size // _SECTOR)

    big_counts = [sectors_for(len(n.data)) for n in big]
    mini_count = sectors_for(len(mini))
    minifat_count = sectors_for(len(minifat) * 4)
    dir_count = sectors_for(len(nodes) * 128)
    data_count = sum(big_counts) + mini_count + minifat_count + dir_count
    fat_count, difat_count = 0, 0
    while True:
        total = data_count + fat_count + difat_count
        need_fat = -(-total // (_SECTOR // 4))
        need_difat = max(0, -(-(need_fat - _HEADER_DIFAT) // (_SECTOR // 4 - 1)))
        if (need_fat, need_difat) == (fat_count, difat_count):
            break
        fat_count, difat_count = need_fat, need_difat

    fat = [_FREESECT] * (fat_count * (_SECTOR // 4))
    cursor = 0

    def place(count: int) -> int:
        nonlocal cursor
        if count == 0:
            return _ENDOFCHAIN
        first = cursor
        for i in range(count - 1):
            fat[first + i] = first + i + 1
        fat[first + count - 1] = _ENDOFCHAIN
        cursor += count
        return first

    for node, count in zip(big, big_counts):
        start_of[id(node)] = place(count)
    mini_start = place(mini_count)
    minifat_start = place(minifat_count)
    dir_start = place(dir_count)
    fat_sectors = list(range(cursor, cursor + fat_count))
    for s in fat_sectors:
        fat[s] = _FATSECT
    cursor += fat_count
    difat_sectors = list(range(cursor, cursor + difat_count))
    for s in difat_sectors:
        fat[s] = _DIFSECT
    cursor += difat_count

    header = bytearray(512)
    header[:8] = SIGNATURE
    struct.pack_into("<HHHHH", header, 0x18, 0x003E, 0x0003, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIII", header, 0x2C, fat_count, dir_start, 0, _MINI_CUTOFF,
                     minifat_start if minifat_count else _ENDOFCHAIN, minifat_count,
                     difat_sectors[0] if difat_sectors else _ENDOFCHAIN, difat_count)
    head_difat = fat_sectors[:_HEADER_DIFAT] + [_FREESECT] * (_HEADER_DIFAT - min(_HEADER_DIFAT, fat_count))
    struct.pack_into(f"<{_HEADER_DIFAT}I", header, 0x4C, *head_difat)

    body = bytearray()
    for node in big:
        body += node.data + b"\0" * (sectors_for(len(node.data)) * _SECTOR - len(node.data))
    body += mini + b"\0" * (mini_count * _SECTOR - len(mini))
    body += struct.pack(f"<{len(minifat)}I", *minifat) + struct.pack("<I", _FREESECT) * (
        minifat_count * (_SECTOR // 4) - len(minifat))

    directory = bytearray()
    for sid, node in enumerate(nodes):
        left, right, child, color = links[sid]
        encoded = node.name.encode("utf-16-le")
        if len(encoded) > 62:
            raise CfbError("항목 이름이 31 글자를 넘음")
        if node.kind == _STREAM:
            start, size = start_of[id(node)], len(node.data)
            created = modified = b"\0" * 8
        elif node.kind == _ROOT:
            start, size = (mini_start if mini_count else _ENDOFCHAIN), len(mini)
            created, modified = node.created, node.modified
        else:
            start, size = 0, 0
            created, modified = node.created, node.modified
        record = bytearray(128)
        record[:len(encoded)] = encoded
        struct.pack_into("<HBB", record, 0x40, len(encoded) + 2, node.kind, color)
        struct.pack_into("<III", record, 0x44, left, right, child)
        record[0x50:0x60] = node.clsid[:16].ljust(16, b"\0")
        struct.pack_into("<I", record, 0x60, node.state)
        record[0x64:0x6C] = created[:8].ljust(8, b"\0")
        record[0x6C:0x74] = modified[:8].ljust(8, b"\0")
        struct.pack_into("<IQ", record, 0x74, start, size)
        directory += record
    empty = bytearray(128)
    struct.pack_into("<III", empty, 0x44, _NOSTREAM, _NOSTREAM, _NOSTREAM)
    while len(directory) < dir_count * _SECTOR:
        directory += empty
    body += directory

    body += struct.pack(f"<{len(fat)}I", *fat)
    rest = fat_sectors[_HEADER_DIFAT:]
    per = _SECTOR // 4 - 1
    for i, sector in enumerate(difat_sectors):
        chunk = rest[i * per:(i + 1) * per]
        chunk += [_FREESECT] * (per - len(chunk))
        nxt = difat_sectors[i + 1] if i + 1 < len(difat_sectors) else _ENDOFCHAIN
        body += struct.pack(f"<{per + 1}I", *chunk, nxt)
    return bytes(header) + bytes(body)


def _tree(sids: List[int], links: Dict[int, List[int]]) -> int:
    """이름 차례로 놓인 형제를 균형 트리로 잇고 뿌리를 돌려준다.

    가운데에서 나누면 빈 자리가 모두 깊이 H 또는 H+1 에 생긴다. 가장 깊은 층을(꽉 차지 않았으면) 빨강으로 칠하면
    모든 경로의 검정 수가 같아 레드-블랙 트리 조건을 지킨다.
    """
    depth: Dict[int, int] = {}

    def build(lo: int, hi: int, level: int) -> int:
        if lo > hi:
            return _NOSTREAM
        mid = (lo + hi + 1) // 2
        sid = sids[mid]
        depth[sid] = level
        links[sid][0] = build(lo, mid - 1, level + 1)
        links[sid][1] = build(mid + 1, hi, level + 1)
        return sid

    root = build(0, len(sids) - 1, 0)
    deepest = max(depth.values())
    perfect = len(sids) == (1 << (deepest + 1)) - 1
    for sid in sids:
        links[sid][3] = _RED if (not perfect and depth[sid] == deepest) else _BLACK
    return root
