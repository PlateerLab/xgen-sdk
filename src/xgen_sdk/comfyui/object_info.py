"""``GET /object_info`` 의 입력 규격을 편집기·검증이 쓰는 한 모양으로 정규화한다.

ComfyUI 가 주는 입력 규격(실제 0.35.0 에서 확인):

    ["INT", {"default": 0, "min": 0, "max": 18446744073709551615, "step": 1}]
    ["FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.1}]
    ["STRING", {"multiline": true, "tooltip": "..."}]
    ["BOOLEAN", {}]
    [["euler", "heun", ...], {"tooltip": "..."}]      # 콤보(목록)
    [[]]                                               # 모델이 없는 로더 콤보
    ["COMBO", {"options": [...]}]                      # 콤보(V3), options 가 없을 수도 있다
    ["COMFY_DYNAMICCOMBO_V3", {"options": [{"key": "...", "inputs": {...}}]}]
    ["MODEL", {}] / ["COMFY_MATCHTYPE_V3", {...}] / ["*", {}]   # 링크형
    ["COLOR", {"default": "#000000"}]                  # 그 밖의 위젯

콤보 선택지가 비어 있을 수 있으므로(모델 미설치) 이 목록으로 값을 강제하지 않는다.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

INPUT_KINDS = ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "LINK", "OTHER")

_PRIMITIVE = {"INT", "FLOAT", "STRING", "BOOLEAN"}
# 링크가 아니라 위젯인데 우리 형(kind)에 딱 맞지 않는 것
_OTHER_WIDGETS = {"COLOR"}
_RAW_CLASS_KEYS = ("output_node", "input_order", "display_name", "category", "python_module")


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def normalize_input_spec(spec: Any) -> Dict[str, Any]:
    """입력 규격 하나 → ``{kind, default, min, max, step, choices, multiline, tooltip}``."""
    out: Dict[str, Any] = {
        "kind": "OTHER",
        "default": None,
        "min": None,
        "max": None,
        "step": None,
        "choices": None,
        "multiline": False,
        "tooltip": None,
    }
    if not isinstance(spec, (list, tuple)) or not spec:
        return out
    head = spec[0]
    opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}

    if isinstance(head, (list, tuple)):
        out["kind"] = "COMBO"
        out["choices"] = list(head)
    elif head == "COMBO":
        out["kind"] = "COMBO"
        options = opts.get("options")
        out["choices"] = list(options) if isinstance(options, (list, tuple)) else []
    elif head == "COMFY_DYNAMICCOMBO_V3":
        out["kind"] = "COMBO"
        options = opts.get("options")
        choices = []
        if isinstance(options, (list, tuple)):
            for option in options:
                if isinstance(option, dict) and isinstance(option.get("key"), str):
                    choices.append(option["key"])
                elif isinstance(option, str):
                    choices.append(option)
        out["choices"] = choices
    elif not isinstance(head, str) or not head:
        out["kind"] = "OTHER"
    elif head in _PRIMITIVE:
        out["kind"] = head
    elif head in _OTHER_WIDGETS:
        out["kind"] = "OTHER"
    else:
        out["kind"] = "LINK"

    if out["kind"] != "LINK":
        default = opts.get("default")
        if isinstance(default, (str, int, float, bool)):
            out["default"] = default
        out["min"] = _number(opts.get("min"))
        out["max"] = _number(opts.get("max"))
        out["step"] = _number(opts.get("step"))
        out["multiline"] = opts.get("multiline") is True
    tooltip = opts.get("tooltip")
    out["tooltip"] = tooltip if isinstance(tooltip, str) and tooltip else None
    return out


def normalize_class_info(entry: Any) -> Dict[str, Dict[str, Any]]:
    """class 하나의 원본 규격 → ``{input_name: InputSpec}`` (required, optional 순)."""
    if not isinstance(entry, dict):
        return {}
    section = entry.get("input")
    if not isinstance(section, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for group in ("required", "optional"):
        inputs = section.get(group)
        if not isinstance(inputs, dict):
            continue
        for name, spec in inputs.items():
            if isinstance(name, str) and name not in out:
                out[name] = normalize_input_spec(spec)
    return out


def normalize_object_info(raw: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``/object_info`` 응답(여러 class) → ``{class: {input: InputSpec}}``.

    hidden 입력은 사용자가 정하는 값이 아니므로 뺀다.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        class_type: normalize_class_info(entry)
        for class_type, entry in raw.items()
        if isinstance(class_type, str) and isinstance(entry, dict)
    }


def _looks_raw(entry: Dict[str, Any]) -> bool:
    if any(key in entry for key in _RAW_CLASS_KEYS):
        return True
    section = entry.get("input")
    return isinstance(section, dict) and "kind" not in section


def as_normalized(object_info: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """원본이든 이미 정규화된 것이든 정규화된 모양으로 맞춘다(class 단위로 판단)."""
    if not isinstance(object_info, dict):
        return {}
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for class_type, entry in object_info.items():
        if not isinstance(class_type, str) or not isinstance(entry, dict):
            continue
        if _looks_raw(entry):
            out[class_type] = normalize_class_info(entry)
        else:
            out[class_type] = {
                name: spec for name, spec in entry.items()
                if isinstance(name, str) and isinstance(spec, dict)
            }
    return out


__all__ = ["INPUT_KINDS", "as_normalized", "normalize_input_spec", "normalize_object_info"]
