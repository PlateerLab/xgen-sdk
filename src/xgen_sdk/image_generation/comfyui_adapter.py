"""ComfyUI 어댑터 — 기존 ``xgen_sdk.comfyui.run_workflow`` 를 공통 결과 모양으로 감싼다.

모델 대신 등록 워크플로우(entry)가 단위다. 연결 · 인증 헤더 · 워크플로우 JSON · 매핑은
호출하는 쪽(xgen-workflow 의 ComfyUI 카탈로그)이 준비해 ``ComfyUIClient`` 로 넘긴다.

편집(참조 이미지 입력): 매핑에 ``image`` 형 파라미터가 있을 때만 지원한다. 매핑 v1 의
형은 ``string · integer · number · boolean`` 뿐이라 **지금은 편집 미지원** —
``supports_edit`` 가 False 를 돌려주고, 참조 이미지를 주면 invalid_input 오류다.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from xgen_sdk.comfyui.client import ComfyUIClient, run_workflow
from xgen_sdk.comfyui.errors import ArgumentError, ComfyUIError
from xgen_sdk.comfyui.mapping import apply_arguments, normalize_mapping
from xgen_sdk.image_generation.errors import (
    CODE_INVALID_INPUT,
    MSG_EDIT_UNSUPPORTED,
    ImageGenerationError,
    from_comfyui_error,
)
from xgen_sdk.image_generation.types import (
    OPERATION_EDIT,
    OPERATION_GENERATE,
    PROVIDER_COMFYUI,
    Capabilities,
    GeneratedImage,
    ImageRequest,
    ImageResult,
)

PROVIDER = PROVIDER_COMFYUI
IMAGE_PARAM_TYPE = "image"  # 매핑 v1 에는 없다 — 생기면 편집이 켜진다


def _raw_params(mapping: Any) -> List[Dict[str, Any]]:
    params = mapping.get("params") if isinstance(mapping, dict) else None
    return [p for p in params if isinstance(p, dict)] if isinstance(params, list) else []


def image_input_params(mapping: Any) -> List[str]:
    return [str(p.get("name")) for p in _raw_params(mapping) if p.get("type") == IMAGE_PARAM_TYPE and p.get("name")]


def supports_edit(mapping: Any) -> bool:
    """매핑에 이미지 입력 파라미터가 있나(매핑 v1 이면 늘 False = 편집 미지원)."""
    return bool(image_input_params(mapping))


def comfyui_capabilities(mapping: Any) -> Capabilities:
    refs = image_input_params(mapping)
    return Capabilities(generate=True, edit=bool(refs), sizes=[], qualities=[], max_n=1, max_reference_images=len(refs))


def _prompt_param(mapping: Dict[str, Any]) -> Optional[str]:
    params = mapping.get("params") or []
    names = [p["name"] for p in params]
    if "prompt" in names:
        return "prompt"
    for p in params:
        if p.get("type") == "string":
            return p["name"]
    return None


def arguments_from_request(mapping: Any, request: ImageRequest) -> Dict[str, Any]:
    """ImageRequest → 매핑 인자. prompt 는 ``prompt`` 파라미터(없으면 첫 문자열 파라미터)로."""
    m = normalize_mapping(mapping)
    name = _prompt_param(m)
    return {name: request.prompt} if name and request.prompt else {}


async def generate(
    client: ComfyUIClient,
    graph: Dict[str, Any],
    mapping: Any,
    request: Optional[ImageRequest] = None,
    *,
    arguments: Optional[Dict[str, Any]] = None,
    timeout_s: float = 300.0,
    max_files: int = 8,
    max_bytes: int = 50_000_000,
    entry_key: str = "",
    rng: Any = None,
) -> ImageResult:
    """워크플로우 한 번 실행 → ImageResult.

    ``arguments`` (도구 인자 그대로)가 있으면 그것을, 없으면 ``request`` 에서 만든다.
    결과 파일은 이미지가 아니어도(영상 등) 모두 ``images`` 에 담긴다 — mime 으로 가른다.
    ``metadata``: ``{prompt_id, files: [{node_id, filename}]}``.

    Raises:
        ImageGenerationError: 모든 실패(``detail.comfyui_code`` 에 원래 code).
    """
    if request is not None and request.reference_images and not supports_edit(mapping):
        raise ImageGenerationError(
            CODE_INVALID_INPUT, MSG_EDIT_UNSUPPORTED,
            detail="This workflow has no image input; reference_images are not supported.", provider=PROVIDER,
        )
    args = dict(arguments) if arguments is not None else (arguments_from_request(mapping, request) if request else {})
    started = time.monotonic()
    try:
        prompt_graph = apply_arguments(graph, mapping, args, rng=rng)
        outputs = normalize_mapping(mapping)["outputs"]
        raw = await run_workflow(
            client, prompt_graph, output_node_ids=outputs, timeout_s=timeout_s, max_files=max_files, max_bytes=max_bytes,
        )
    except (ArgumentError, ComfyUIError) as exc:
        raise from_comfyui_error(exc) from exc
    files = raw.get("files") or []
    images = [
        GeneratedImage(data=f["data"], mime=f.get("content_type") or "application/octet-stream", filename=f.get("filename"))
        for f in files
    ]
    return ImageResult(
        images=images,
        usage=None,
        model=entry_key,
        latency_ms=int(raw.get("duration_ms") or (time.monotonic() - started) * 1000),
        provider=PROVIDER,
        operation=OPERATION_EDIT if (request is not None and request.reference_images) else OPERATION_GENERATE,
        metadata={
            "prompt_id": raw.get("prompt_id"),
            "files": [{"node_id": f.get("node_id"), "filename": f.get("filename")} for f in files],
        },
    )


__all__ = [
    "IMAGE_PARAM_TYPE",
    "arguments_from_request",
    "comfyui_capabilities",
    "generate",
    "image_input_params",
    "supports_edit",
]
