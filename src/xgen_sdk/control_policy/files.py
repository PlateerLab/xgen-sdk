"""파일 바이트 가리기: 형식마다 글이 있는 자리만 바꾼다.

쓰는 곳:

    xgen-documents  출처 원본 서빙(컬렉션 문서 미리보기) · 파일 저장소 업로드 제거
    xgen-workflow   채팅 파일 업로드 제거(작업 공간 · 고정본 대화 · 배포 API · Teams 첨부)

    형식                                   방법
    txt · md · csv · json · 코드 등         인코딩을 지켜 글 전체에 적용
    html · htm · xhtml                     텍스트 노드만(태그 · 속성 · 인라인 그림은 그대로)
    docx · xlsx · pptx (+ 매크로 변형)      문단(w:p · a:p · si)마다 텍스트 노드를 이어 찾고, 노드별로 되돌려 쓴다
    pdf                                    페이지 글에서 찾아 좌표로 redaction(글자를 실제로 지운다)

글을 읽을 수 없는 형식(그림 · 한글 · 옛 Office · 압축 등), 텍스트 레이어가 없는 PDF(스캔), 암호 PDF,
상한(:data:`MASKING_MAX_BYTES`)을 넘는 파일은 :class:`MaskingUnsupportedError`. 그때 어떻게 할지(원본 거절 ·
그대로 둠)는 부르는 쪽이 정한다.

무거운 의존(xgen-pdf · beautifulsoup4 · chardet)은 함수 안에서 늦게 불러온다: 이 SDK 를 쓰는 서비스가 다
가진 것은 아니다(core 는 파일을 가리지 않는다). 없으면 그 형식만 :class:`MaskingUnsupportedError`.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape, unescape as xml_unescape

from .rules import MaskingResult, MaskingRule, apply_rules, find_masked_spans

#: 가리려면 파일 전체를 메모리에 올려야 해서 상한을 둔다. 옛 이름(documents)도 읽는다.
MASKING_MAX_BYTES = int(
    os.getenv("XGEN_FILE_MASKING_MAX_BYTES")
    or os.getenv("DOCUMENT_SOURCE_MASKING_MAX_BYTES")
    or str(32 * 1024 * 1024)
)


class MaskingUnsupportedError(Exception):
    """이 파일을 가릴 수단이 없다(형식 · 스캔본 · 암호 · 상한)."""

    def __init__(self, extension: str, reason: str = "지원하지 않는 형식"):
        self.extension = extension
        self.reason = reason
        super().__init__(f"'{extension or '알 수 없음'}' 형식은 가릴 수 없습니다 ({reason}).")


def extension_of(file_name: str) -> str:
    base = os.path.basename(file_name or "")
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


# ── 글 형식 ──────────────────────────────────────────────────────────


def _decode(raw: bytes) -> Tuple[str, str]:
    encodings = ["utf-8"]
    try:
        import chardet

        detected = chardet.detect(raw[:64 * 1024]).get("encoding")
        if detected:
            encodings.append(detected)
    except Exception:  # noqa: BLE001, chardet 이 없으면 utf-8 · cp949 만
        pass
    encodings.append("cp949")
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def _encode(text: str, encoding: str) -> bytes:
    # 원래 인코딩을 지킨다: meta charset 이 박힌 문서가 깨지지 않게.
    try:
        return text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return text.encode("utf-8")


def mask_text(text: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    result = apply_rules(text, rules)
    return result.text, result.total_detected


def _mask_plain_text(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    text, encoding = _decode(raw)
    masked, hits = mask_text(text, rules)
    return (_encode(masked, encoding) if hits else raw), hits


def mask_html_document(html: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    """HTML 텍스트 노드만: 태그 · 속성(인라인 base64 그림 등)은 건드리지 않는다."""
    if not html or not rules:
        return html, 0
    try:
        from bs4 import BeautifulSoup, NavigableString
    except ImportError as exc:  # pragma: no cover, 설치 환경 문제
        raise MaskingUnsupportedError("html", "HTML 처리기 없음") from exc

    soup = BeautifulSoup(html, "html.parser")
    hits = 0
    for node in soup.find_all(string=True):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent.name if node.parent else ""
        if parent in {"script", "style"}:
            continue
        masked, count = mask_text(str(node), rules)
        if count:
            node.replace_with(masked)
            hits += count
    return (str(soup) if hits else html), hits


def _html_text(raw: bytes) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return ""
    return BeautifulSoup(_decode(raw)[0], "html.parser").get_text("\n")


def _mask_html(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    text, encoding = _decode(raw)
    masked, hits = mask_html_document(text, rules)
    return (_encode(masked, encoding) if hits else raw), hits


# ── Office(OOXML) ───────────────────────────────────────────────────

#: 문자 데이터 태그(w:t / a:t / t).
_OOXML_TEXT_RE = re.compile(r"(<(?:w:t|a:t|t)(?:\s[^>]*)?>)([^<]*)(</(?:w:t|a:t|t)>)")
#: 문단 컨테이너. Word 는 한 문장을 여러 run 으로 쪼개므로 노드별로 보면 매치를 놓친다.
_OOXML_PARA_RE = re.compile(r"<(w:p|a:p|si)(?:\s[^>]*)?>.*?</\1>", re.S)


def _mask_ooxml_fragment(fragment: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    """문단 조각 하나: 텍스트 노드를 이어 붙여 찾고 노드별로 되돌려 쓴다."""
    nodes = list(_OOXML_TEXT_RE.finditer(fragment))
    if not nodes:
        return fragment, 0
    pieces = [xml_unescape(node.group(2)) for node in nodes]
    spans = find_masked_spans("".join(pieces), rules)
    if not spans:
        return fragment, 0

    # 마스크는 매치가 시작하는 노드에만 넣고 나머지 노드에서는 지운다: 매치 밖 서식은 그대로.
    rebuilt: List[str] = []
    offset = 0
    for piece in pieces:
        start, end = offset, offset + len(piece)
        offset = end
        out: List[str] = []
        cursor = start
        for s_start, s_end, mask in spans:
            if s_end <= start or s_start >= end:
                continue
            if cursor < min(s_start, end):
                out.append(piece[cursor - start: min(s_start, end) - start])
            if s_start >= start:
                out.append(mask)
            cursor = max(cursor, min(s_end, end))
        if cursor < end:
            out.append(piece[cursor - start:])
        rebuilt.append(xml_escape("".join(out)))

    result: List[str] = []
    last = 0
    for node, text in zip(nodes, rebuilt):
        result.append(fragment[last:node.start(2)])
        result.append(text)
        last = node.end(2)
    result.append(fragment[last:])
    return "".join(result), len(spans)


def _mask_ooxml_part(xml: str, rules: Sequence[MaskingRule]) -> Tuple[str, int]:
    """문단 블록과 그 사이 구간을 각각: 문단 밖 텍스트 노드도 놓치지 않는다."""
    segments: List[str] = []
    hits = 0
    last = 0
    for para in _OOXML_PARA_RE.finditer(xml):
        for chunk in (xml[last:para.start()], para.group(0)):
            masked, count = _mask_ooxml_fragment(chunk, rules)
            segments.append(masked)
            hits += count
        last = para.end()
    masked, count = _mask_ooxml_fragment(xml[last:], rules)
    segments.append(masked)
    hits += count
    return "".join(segments), hits


def _mask_ooxml(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    """OOXML(zip) 안 XML 파트의 문자 데이터만 문단 단위로 바꾼다. 파트는 UTF-8 고정(ECMA-376)."""
    hits = 0
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename.lower().endswith(".xml"):
                    masked_xml, count = _mask_ooxml_part(data.decode("utf-8"), rules)
                    hits += count
                    data = masked_xml.encode("utf-8")
                target.writestr(item, data)
    return (output.getvalue() if hits else raw), hits


def _ooxml_text(raw: bytes) -> str:
    paragraphs: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        for item in source.infolist():
            if not item.filename.lower().endswith(".xml"):
                continue
            xml = source.read(item.filename).decode("utf-8")
            for para in _OOXML_PARA_RE.finditer(xml):
                pieces = [xml_unescape(node.group(2)) for node in _OOXML_TEXT_RE.finditer(para.group(0))]
                if pieces:
                    paragraphs.append("".join(pieces))
    return "\n".join(paragraphs)


# ── PDF ──────────────────────────────────────────────────────────────


def _pdf_engine():
    try:
        import xgen_pdf
    except ImportError as exc:
        raise MaskingUnsupportedError("pdf", "PDF 엔진 없음") from exc
    return xgen_pdf


def _redaction_label(mask: str) -> str:
    # 내장 base14 글꼴로 못 그리는 마스크(한글 등)는 글자 없이 상자만 남긴다.
    return mask if mask.isascii() else ""


def _mask_pdf(raw: bytes, rules: Sequence[MaskingRule]) -> Tuple[bytes, int]:
    """페이지 글에서 매치를 찾아 좌표를 되짚은 뒤 redaction 으로 실제로 지운다."""
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
            for rule in rules:
                for match in rule.pattern.finditer(text):
                    matched = match.group(0)
                    # 상자 위 글자는 매치별 치환문. 줄 수가 어긋나면(형식을 지키지 않는 부분 마스킹 등)
                    # 글자 없이 상자만: 원문을 다시 그리지 않는다.
                    replacement_lines = rule.mask_match(matched).splitlines()
                    for idx, line in enumerate(matched.splitlines()):
                        literal = line.strip()
                        # search_for 는 줄바꿈을 넘지 못해 줄마다 찾는다.
                        if not literal:
                            continue
                        label = replacement_lines[idx].strip() if idx < len(replacement_lines) else ""
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
        return (document.tobytes(garbage=3, deflate=True) if hits else raw), hits
    finally:
        document.close()


def _pdf_text(raw: bytes) -> str:
    pdf = _pdf_engine()
    document = pdf.open(stream=raw, filetype="pdf")
    try:
        if document.needs_pass:
            return ""
        return "\n".join(page.get_text("text") or "" for page in document)
    finally:
        document.close()


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
        lambda raw: _decode(raw)[0],
    ),
)

_MASKER_BY_EXTENSION: Dict[str, FormatMasker] = {
    extension: masker for masker in FORMAT_MASKERS for extension in masker.extensions
}


def masker_for(file_name_or_extension: str) -> Optional[FormatMasker]:
    """이 파일(이름 또는 확장자)을 가릴 수단. 없으면 None."""
    key = (file_name_or_extension or "").lower()
    if "." in key:
        key = extension_of(key)
    return _MASKER_BY_EXTENSION.get(key)


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

    지울 수 없거나 걸린 것이 없으면 파일을 건드리지 않는다. 상한을 넘는 파일은 읽지도 않는다.
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
        with open(path, "wb") as stream:
            stream.write(result.content)
    return result
