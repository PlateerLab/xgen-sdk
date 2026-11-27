"""이미지 생성 공용 타입.

모두 dataclass 이고, 계약 문서의 dict 모양으로도 읽힌다 — ``result.images[0].data``
와 ``result["images"][0]["data"]`` 가 같다. 저장 · 로그 · 응답에 실을 때는
``to_dict()`` 를 쓴다(이미지 바이트는 ``include_data=False`` 로 뺄 수 있다).

비밀값은 ``Credentials.api_key`` 에만 있고 repr · to_dict 어디에도 원문이 나오지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

PROVIDER_OPENAI = "openai"
PROVIDER_GEMINI = "gemini"
PROVIDER_COMFYUI = "comfyui"
# 표시 순서 = 계약의 sort_order (openai 10 · gemini 20 · comfyui 90)
PROVIDERS = (PROVIDER_OPENAI, PROVIDER_GEMINI, PROVIDER_COMFYUI)
PROVIDER_KINDS = {
    PROVIDER_OPENAI: "openai_images",
    PROVIDER_GEMINI: "gemini_image",
    PROVIDER_COMFYUI: "comfyui",
}
PROVIDER_DISPLAY_NAMES = {
    PROVIDER_OPENAI: "OpenAI",
    PROVIDER_GEMINI: "Gemini",
    PROVIDER_COMFYUI: "ComfyUI",
}
PROVIDER_SORT_ORDER = {PROVIDER_OPENAI: 10, PROVIDER_GEMINI: 20, PROVIDER_COMFYUI: 90}

OPERATION_GENERATE = "generate"
OPERATION_EDIT = "edit"


class _DictView:
    """dataclass 를 계약의 dict 모양으로도 읽게 한다(읽기 전용)."""

    def __getitem__(self, key: str) -> Any:
        if key in self._keys():
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key) if key in self._keys() else default

    def __contains__(self, key: object) -> bool:
        return key in self._keys()

    def keys(self) -> List[str]:
        return list(self._keys())

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys())

    def _keys(self) -> Sequence[str]:
        return [f.name for f in fields(self)]  # type: ignore[arg-type]


@dataclass
class ReferenceImage(_DictView):
    """편집에 쓰는 참조 이미지 — 원본 바이트와 MIME."""

    data: bytes
    mime: str = "image/png"

    def __repr__(self) -> str:
        return f"ReferenceImage(mime={self.mime!r}, bytes={len(self.data or b'')})"


RefLike = Union[ReferenceImage, Dict[str, Any]]


@dataclass
class ImageRequest(_DictView):
    """생성 · 편집 요청. ``reference_images`` 가 있으면 편집(edit)이다.

    size / quality 는 모델 capabilities 의 값(openai: ``1024x1024`` · ``high``,
    gemini: 화면비 ``16:9`` · 해상도 ``2K``). 비우면 제공자 기본값.
    """

    prompt: str
    n: int = 1
    size: Optional[str] = None
    quality: Optional[str] = None
    reference_images: List[RefLike] = field(default_factory=list)

    def __post_init__(self) -> None:
        refs: List[ReferenceImage] = []
        for item in self.reference_images or []:
            if isinstance(item, ReferenceImage):
                refs.append(item)
            elif isinstance(item, dict):
                refs.append(ReferenceImage(data=item.get("data") or b"", mime=item.get("mime") or "image/png"))
            else:
                raise TypeError("reference_images items must be ReferenceImage or {data, mime} dicts")
        self.reference_images = refs  # type: ignore[assignment]

    @property
    def operation(self) -> str:
        return OPERATION_EDIT if self.reference_images else OPERATION_GENERATE

    def __repr__(self) -> str:  # 프롬프트 원문은 남기지 않는다
        return (
            f"ImageRequest(prompt_chars={len(self.prompt or '')}, n={self.n}, size={self.size!r}, "
            f"quality={self.quality!r}, reference_count={len(self.reference_images)})"
        )


@dataclass
class GeneratedImage(_DictView):
    """결과 파일 하나. ``filename`` · ``revised_prompt`` 는 제공자가 줄 때만."""

    data: bytes
    mime: str = "image/png"
    filename: Optional[str] = None
    revised_prompt: Optional[str] = None

    def __repr__(self) -> str:
        return f"GeneratedImage(mime={self.mime!r}, bytes={len(self.data or b'')}, filename={self.filename!r})"

    def to_dict(self, *, include_data: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {"mime": self.mime}
        if include_data:
            out["data"] = self.data
        if self.filename:
            out["filename"] = self.filename
        if self.revised_prompt:
            out["revised_prompt"] = self.revised_prompt
        return out


@dataclass
class ImageUsage(_DictView):
    """토큰 사용량. 제공자가 주지 않은 칸은 None.

    ``image_tokens`` = **출력 이미지** 토큰(openai: output_tokens_details.image_tokens,
    없으면 output_tokens / gemini: candidatesTokensDetails 의 IMAGE 합).
    """

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    image_tokens: Optional[int] = None

    def to_dict(self) -> Dict[str, Optional[int]]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "image_tokens": self.image_tokens}

    def add(self, other: Optional["ImageUsage"]) -> "ImageUsage":
        if other is None:
            return self

        def _sum(a: Optional[int], b: Optional[int]) -> Optional[int]:
            if a is None and b is None:
                return None
            return int(a or 0) + int(b or 0)

        return ImageUsage(
            input_tokens=_sum(self.input_tokens, other.input_tokens),
            output_tokens=_sum(self.output_tokens, other.output_tokens),
            image_tokens=_sum(self.image_tokens, other.image_tokens),
        )


@dataclass
class ImageResult(_DictView):
    """생성 결과. ``metadata`` 는 제공자별 부가 정보(comfyui: prompt_id 등)."""

    images: List[GeneratedImage]
    usage: Optional[ImageUsage] = None
    model: str = ""
    latency_ms: int = 0
    provider: str = ""
    operation: str = OPERATION_GENERATE
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_data: bool = True) -> Dict[str, Any]:
        return {
            "images": [img.to_dict(include_data=include_data) for img in self.images],
            "usage": self.usage.to_dict() if self.usage else None,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "provider": self.provider,
            "operation": self.operation,
            "metadata": dict(self.metadata),
        }


@dataclass
class Credentials(_DictView):
    """``resolve_credentials`` 결과. source: ``custom`` · ``llm`` · ``none``.

    ``error`` 는 개별 키를 풀지 못했을 때 ``secret_unreadable`` (그때 source 는 none).
    """

    api_key: Optional[str] = field(default=None, repr=False)
    base_url: Optional[str] = None
    source: str = "none"
    error: Optional[str] = None

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        return f"Credentials(source={self.source!r}, base_url={self.base_url!r}, has_key={self.has_key})"

    def to_dict(self) -> Dict[str, Any]:
        """원문 키 없이 — 화면 · 로그용."""
        return {"source": self.source, "base_url": self.base_url, "has_key": self.has_key, "error": self.error}


@dataclass
class Capabilities(_DictView):
    """모델이 할 수 있는 일 — ``image_model_catalog.capabilities`` JSON 그대로."""

    generate: bool = True
    edit: bool = False
    sizes: List[str] = field(default_factory=list)
    qualities: List[str] = field(default_factory=list)
    max_n: int = 1
    max_reference_images: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generate": self.generate,
            "edit": self.edit,
            "sizes": list(self.sizes),
            "qualities": list(self.qualities),
            "max_n": self.max_n,
            "max_reference_images": self.max_reference_images,
        }


@dataclass
class ModelInfo(_DictView):
    """모델 목록 한 줄. source: ``sync``(제공자 API) · ``fallback``(정적 목록)."""

    provider: str
    model_id: str
    display_name: str
    capabilities: Capabilities
    source: str = "sync"
    recommended: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "capabilities": self.capabilities.to_dict(),
            "source": self.source,
            "recommended": self.recommended,
        }


__all__ = [
    "Capabilities",
    "Credentials",
    "GeneratedImage",
    "ImageRequest",
    "ImageResult",
    "ImageUsage",
    "ModelInfo",
    "OPERATION_EDIT",
    "OPERATION_GENERATE",
    "PROVIDERS",
    "PROVIDER_COMFYUI",
    "PROVIDER_DISPLAY_NAMES",
    "PROVIDER_GEMINI",
    "PROVIDER_KINDS",
    "PROVIDER_OPENAI",
    "PROVIDER_SORT_ORDER",
    "ReferenceImage",
]
