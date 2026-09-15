"""ComfyUI API 형식 워크플로우 읽기.

API 형식(``Export (API)``)은 ``{node_id: {"inputs": {...}, "class_type": str,
"_meta": {"title": str}}}`` 이다. 입력값이 ``[node_id, output_index]`` 이면 다른
노드의 출력에 연결된 **링크**이고, 그 밖의 값은 위젯 값(고정값)이다.

UI 형식(편집기 저장본, ``nodes``/``links`` 목록)은 실행할 수 없으므로 받지 않는다.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from xgen_sdk.comfyui.errors import WorkflowFormatError

# 결과를 내는 노드로 알려진 이름 (object_info 가 없을 때의 추정)
KNOWN_OUTPUT_CLASSES = frozenset({
    "SaveImage",
    "PreviewImage",
    "SaveAnimatedWEBP",
    "SaveAnimatedPNG",
    "SaveVideo",
})

_DIGITS = re.compile(r"(\d+)")


def natural_key(node_id: str) -> Tuple[Tuple[int, int, str], ...]:
    """``"46" < "90:71" < "90:100" < "91"`` 순서를 주는 정렬 키."""
    parts = []
    for piece in _DIGITS.split(str(node_id)):
        if not piece:
            continue
        if piece.isdigit():
            parts.append((0, int(piece), ""))
        else:
            parts.append((1, 0, piece))
    return tuple(parts)


def is_link(value: Any) -> bool:
    """``["90:73", 0]`` 모양이면 링크다."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def infer_type(value: Any) -> str:
    """값 모양으로 추정한 파라미터 형: string|integer|number|boolean|unknown."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "unknown"


def parse_workflow(raw: Any) -> Dict[str, Dict[str, Any]]:
    """API 형식 워크플로우를 검사해 깊은 복사로 돌려준다.

    Raises:
        WorkflowFormatError: code ``invalid_json`` / ``ui_format`` / ``empty`` /
            ``invalid_node`` (node_id 포함).
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkflowFormatError("invalid_json", "워크플로우 파일을 JSON으로 읽을 수 없습니다.") from None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            raise WorkflowFormatError("invalid_json", "워크플로우 파일을 JSON으로 읽을 수 없습니다.") from None
    else:
        data = raw

    if not isinstance(data, dict):
        raise WorkflowFormatError("invalid_json", "워크플로우 JSON은 노드 id를 키로 가진 객체여야 합니다.")

    if _looks_like_ui_format(data):
        raise WorkflowFormatError("ui_format", "API 형식으로 내보낸 JSON을 넣어 주세요.")

    if not data:
        raise WorkflowFormatError("empty", "워크플로우에 노드가 없습니다.")

    for node_id, node in data.items():
        if not isinstance(node_id, str) or not node_id:
            raise WorkflowFormatError(
                "invalid_node", "노드 id는 비어 있지 않은 문자열이어야 합니다.", node_id=str(node_id),
            )
        if not isinstance(node, dict):
            raise WorkflowFormatError(
                "invalid_node", f"{node_id} 노드의 모양이 API 형식과 다릅니다.", node_id=node_id,
            )
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type.strip():
            raise WorkflowFormatError(
                "invalid_node", f"{node_id} 노드에 class_type이 없습니다.", node_id=node_id,
            )
        if not isinstance(node.get("inputs"), dict):
            raise WorkflowFormatError(
                "invalid_node", f"{node_id} 노드에 inputs 객체가 없습니다.", node_id=node_id,
            )
        meta = node.get("_meta")
        if meta is not None and not isinstance(meta, dict):
            raise WorkflowFormatError(
                "invalid_node", f"{node_id} 노드의 _meta가 객체가 아닙니다.", node_id=node_id,
            )

    return copy.deepcopy(data)


def _looks_like_ui_format(data: Dict[str, Any]) -> bool:
    for key in ("nodes", "links"):
        if key in data and not _is_node_shaped(data[key]):
            return True
    return False


def _is_node_shaped(value: Any) -> bool:
    return isinstance(value, dict) and "class_type" in value


def node_title(node: Dict[str, Any]) -> str:
    meta = node.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    if isinstance(title, str) and title.strip():
        return title
    return str(node.get("class_type") or "")


def sorted_node_ids(graph: Dict[str, Any]) -> List[str]:
    return sorted(graph.keys(), key=natural_key)


def list_nodes(graph: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """편집기가 그릴 노드 목록.

    ``{node_id, class_type, title, inputs:[{name, value, is_link, link,
    inferred_type}]}`` — node_id 자연 정렬, 입력은 워크플로우에 적힌 순서.
    ``title`` 은 ``_meta.title``, 없으면 class_type.
    """
    out: List[Dict[str, Any]] = []
    for node_id in sorted_node_ids(graph):
        node = graph[node_id] if isinstance(graph[node_id], dict) else {}
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        items = []
        for name, value in inputs.items():
            link = is_link(value)
            items.append({
                "name": name,
                "value": copy.deepcopy(value),
                "is_link": link,
                "link": [value[0], value[1]] if link else None,
                "inferred_type": "unknown" if link else infer_type(value),
            })
        out.append({
            "node_id": node_id,
            "class_type": str(node.get("class_type") or ""),
            "title": node_title(node),
            "inputs": items,
        })
    return out


def _raw_output_flag(object_info: Optional[Dict[str, Any]], class_type: str) -> Optional[bool]:
    """object_info(원본 ``/object_info`` 모양)가 이 class 의 output_node 를 알려 주면 그 값."""
    if not isinstance(object_info, dict):
        return None
    entry = object_info.get(class_type)
    if isinstance(entry, dict) and "output_node" in entry:
        return bool(entry.get("output_node"))
    return None


def guess_output_class(class_type: str) -> bool:
    return (
        class_type in KNOWN_OUTPUT_CLASSES
        or class_type.startswith("Save")
        or class_type.startswith("Preview")
    )


def output_node_candidates(
    graph: Dict[str, Dict[str, Any]],
    object_info: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """결과를 내는 노드 id 목록(자연 정렬).

    object_info 에 그 class 의 ``output_node`` 가 있으면 그것을 따르고, 없으면
    알려진 이름(``SaveImage`` 등, ``Save``/``Preview`` 로 시작)으로 추정한다.
    정규화된 object_info(``output_node`` 없음)를 넘겨도 이름 추정으로 동작한다.
    """
    out = []
    for node_id in sorted_node_ids(graph):
        node = graph[node_id]
        class_type = str(node.get("class_type") or "") if isinstance(node, dict) else ""
        if not class_type:
            continue
        flag = _raw_output_flag(object_info, class_type)
        if flag is None:
            flag = guess_output_class(class_type)
        if flag:
            out.append(node_id)
    return out


__all__ = [
    "KNOWN_OUTPUT_CLASSES",
    "infer_type",
    "is_link",
    "list_nodes",
    "natural_key",
    "output_node_candidates",
    "parse_workflow",
]
