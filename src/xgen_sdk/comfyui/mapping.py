"""워크플로우 항목의 **매핑**(어느 입력을 도구 파라미터로 노출하나) 규칙.

모양 (``param_mapping``)::

    {"version": 1,
     "params": [{"name": "prompt", "description": "...", "type": "string",
                 "required": true, "default": null, "multiline": true,
                 "minimum": null, "maximum": null, "choices": null,
                 "randomize": false,
                 "targets": [{"node_id": "90:77", "input": "text"}]}],
     "outputs": ["46"]}

노출하지 않은 입력은 워크플로우 JSON 에 적힌 값이 곧 고정값이다. 노출했지만
인자를 비운 입력도 기본값이나 무작위가 없으면 워크플로우 값을 그대로 쓴다.

검증 이슈 message 는 관리 화면에 싣는 한국어 한 문장이고, 인자 오류
(``ArgumentError``) 는 모델이 읽고 고칠 영어 문장이다.
"""
from __future__ import annotations

import copy
import json
import math
import random
import re
from typing import Any, Dict, List, Optional

from xgen_sdk.comfyui.errors import ArgumentError
from xgen_sdk.comfyui.object_info import as_normalized
from xgen_sdk.comfyui.workflow import infer_type, is_link

MAPPING_VERSION = 1

PARAM_TYPES = ("string", "integer", "number", "boolean")
PARAM_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
MAX_PARAMS = 30
MAX_DESCRIPTION_LENGTH = 500
RANDOM_MAX = 2 ** 53 - 1

ISSUE_CODES = (
    "name_invalid",
    "name_duplicate",
    "too_many_params",
    "targets_empty",
    "target_missing_node",
    "target_missing_input",
    "target_is_link",
    "target_duplicate",
    "type_mismatch",
    "default_out_of_range",
    "default_not_in_choices",
    "default_type",
    "choices_empty",
    "outputs_empty",
    "output_not_found",
    "randomize_not_integer",
    "description_too_long",
)

_NAME_RE = re.compile(PARAM_NAME_PATTERN)
_INT_RE = re.compile(r"^[+-]?\d+$")

_TYPE_KO = {"string": "문자열", "integer": "정수", "number": "숫자", "boolean": "참/거짓"}
_KIND_KO = {"INT": "정수", "FLOAT": "숫자", "STRING": "문자열", "BOOLEAN": "참/거짓", "COMBO": "선택지"}
# object_info kind → 받아들이는 파라미터 형
_KIND_ACCEPTS = {
    "INT": {"integer"},
    "FLOAT": {"number", "integer"},
    "STRING": {"string"},
    "BOOLEAN": {"boolean"},
    "COMBO": {"string", "integer", "number"},
}
# 규격이 없을 때: 워크플로우에 적힌 값 모양 → 받아들이는 파라미터 형
_VALUE_ACCEPTS = {
    "string": {"string"},
    "integer": {"integer", "number"},
    "number": {"number"},
    "boolean": {"boolean"},
}


# ─── 값 판정 ──────────────────────────────────────────────────────
def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not (isinstance(value, float) and not math.isfinite(value))
    )


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def _matches_type(value: Any, ptype: str) -> bool:
    if ptype == "string":
        return isinstance(value, str)
    if ptype == "integer":
        return _is_integer(value)
    if ptype == "number":
        return _is_number(value)
    if ptype == "boolean":
        return isinstance(value, bool)
    return False


def _flag(value: Any) -> bool:
    return value is True


# ─── 정규화 ──────────────────────────────────────────────────────
def _normalize_target(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {"node_id": "", "input": ""}
    node_id = raw.get("node_id")
    if isinstance(node_id, int) and not isinstance(node_id, bool):
        node_id = str(node_id)
    name = raw.get("input")
    return {
        "node_id": node_id if isinstance(node_id, str) else "",
        "input": name if isinstance(name, str) else "",
    }


def _normalize_param(raw: Any) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    ptype = src.get("type")
    choices = src.get("choices")
    targets = src.get("targets")
    description = src.get("description")
    name = src.get("name")
    return {
        "name": name if isinstance(name, str) else "",
        "description": description if isinstance(description, str) else "",
        "type": ptype if isinstance(ptype, str) and ptype else "string",
        "required": _flag(src.get("required")),
        "default": copy.deepcopy(src.get("default")),
        "multiline": _flag(src.get("multiline")),
        "minimum": src.get("minimum"),
        "maximum": src.get("maximum"),
        "choices": list(choices) if isinstance(choices, (list, tuple)) else None,
        "randomize": _flag(src.get("randomize")),
        "targets": [_normalize_target(t) for t in targets] if isinstance(targets, (list, tuple)) else [],
    }


def normalize_mapping(raw: Any) -> Dict[str, Any]:
    """빠진 필드를 기본값으로 채운 매핑. 모르는 키는 버린다.

    JSON 문자열도 받는다. 읽을 수 없으면 빈 매핑(검증에서 ``outputs_empty``).
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    src = raw if isinstance(raw, dict) else {}
    version = src.get("version")
    params = src.get("params")
    outputs = src.get("outputs")
    out_ids: List[str] = []
    if isinstance(outputs, (list, tuple)):
        for item in outputs:
            if isinstance(item, str) and item:
                out_ids.append(item)
            elif isinstance(item, int) and not isinstance(item, bool):
                out_ids.append(str(item))
    return {
        "version": version if isinstance(version, int) and not isinstance(version, bool) else MAPPING_VERSION,
        "params": [_normalize_param(p) for p in params] if isinstance(params, (list, tuple)) else [],
        "outputs": out_ids,
    }


# ─── 검증 ────────────────────────────────────────────────────────
def _issue(path: str, code: str, message: str) -> Dict[str, str]:
    return {"path": path, "code": code, "message": message}


def validate_entry(
    graph: Dict[str, Dict[str, Any]],
    mapping: Any,
    object_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """매핑이 워크플로우와 맞는지 본다. 빈 목록이면 통과.

    object_info 는 원본 ``/object_info`` 모양이든 ``normalize_object_info`` 결과든
    받는다. 규격을 모르는 입력(커스텀 노드 등)은 워크플로우에 적힌 값 모양으로
    형을 추정한다.
    """
    graph = graph if isinstance(graph, dict) else {}
    m = normalize_mapping(mapping)
    specs = as_normalized(object_info)
    issues: List[Dict[str, str]] = []
    params = m["params"]

    if len(params) > MAX_PARAMS:
        issues.append(_issue("params", "too_many_params", f"노출할 수 있는 파라미터는 {MAX_PARAMS}개까지입니다."))

    seen_names: Dict[str, int] = {}
    seen_targets: Dict[tuple, int] = {}
    for i, p in enumerate(params):
        base = f"params[{i}]"
        name = p["name"]
        label = name or f"{i + 1}번째"
        if not _NAME_RE.match(name):
            issues.append(_issue(
                f"{base}.name", "name_invalid",
                "파라미터 이름은 영어 소문자로 시작하고 소문자, 숫자, 밑줄만 써서 40자 이내로 적어야 합니다.",
            ))
        elif name in seen_names:
            issues.append(_issue(f"{base}.name", "name_duplicate", f"{name} 이름을 쓰는 파라미터가 이미 있습니다."))
        else:
            seen_names[name] = i

        ptype = p["type"]
        type_ok = ptype in PARAM_TYPES
        if not type_ok:
            issues.append(_issue(
                f"{base}.type", "type_mismatch",
                "파라미터 형식은 string, integer, number, boolean 중 하나여야 합니다.",
            ))

        if len(p["description"]) > MAX_DESCRIPTION_LENGTH:
            issues.append(_issue(
                f"{base}.description", "description_too_long",
                f"{label} 파라미터의 설명은 {MAX_DESCRIPTION_LENGTH}자 이내로 적어야 합니다.",
            ))

        if p["randomize"] and ptype != "integer":
            issues.append(_issue(
                f"{base}.randomize", "randomize_not_integer",
                f"{label} 파라미터는 정수 형식일 때만 비우면 무작위로 정할 수 있습니다.",
            ))

        minimum, maximum = p["minimum"], p["maximum"]
        numeric = ptype in ("integer", "number")
        for key, bound in (("minimum", minimum), ("maximum", maximum)):
            if bound is not None and not _is_number(bound):
                issues.append(_issue(f"{base}.{key}", "type_mismatch", f"{label} 파라미터의 최솟값과 최댓값은 숫자여야 합니다."))
        if _is_number(minimum) and _is_number(maximum) and minimum > maximum:
            issues.append(_issue(
                f"{base}.minimum", "default_out_of_range", f"{label} 파라미터의 최솟값이 최댓값보다 큽니다.",
            ))

        choices = p["choices"]
        if choices is not None:
            if ptype == "boolean":
                issues.append(_issue(f"{base}.choices", "type_mismatch", "참/거짓 파라미터에는 선택지를 둘 수 없습니다."))
            elif not choices:
                issues.append(_issue(f"{base}.choices", "choices_empty", f"{label} 파라미터의 선택지가 비어 있습니다."))
            elif type_ok:
                for k, choice in enumerate(choices):
                    if not _matches_type(choice, ptype):
                        issues.append(_issue(
                            f"{base}.choices[{k}]", "type_mismatch",
                            f"{label} 파라미터의 선택지에 {_TYPE_KO[ptype]} 형식이 아닌 값이 있습니다.",
                        ))

        default = p["default"]
        if default is not None and type_ok:
            if not _matches_type(default, ptype):
                issues.append(_issue(
                    f"{base}.default", "default_type",
                    f"{label} 파라미터의 기본값이 {_TYPE_KO[ptype]} 형식이 아닙니다.",
                ))
            elif numeric and (
                (_is_number(minimum) and default < minimum) or (_is_number(maximum) and default > maximum)
            ):
                issues.append(_issue(
                    f"{base}.default", "default_out_of_range", f"{label} 파라미터의 기본값이 허용 범위를 벗어났습니다.",
                ))
            elif choices and ptype != "boolean" and default not in choices:
                issues.append(_issue(
                    f"{base}.default", "default_not_in_choices", f"{label} 파라미터의 기본값이 선택지에 없습니다.",
                ))

        if not p["targets"]:
            issues.append(_issue(
                f"{base}.targets", "targets_empty", f"{label} 파라미터가 바꿀 노드 입력을 하나 이상 골라야 합니다.",
            ))
        for j, target in enumerate(p["targets"]):
            tpath = f"{base}.targets[{j}]"
            node_id, input_name = target["node_id"], target["input"]
            node = graph.get(node_id) if node_id else None
            if not isinstance(node, dict):
                issues.append(_issue(f"{tpath}.node_id", "target_missing_node", f"{node_id or '선택한'} 노드가 워크플로우에 없습니다."))
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            if input_name not in inputs:
                issues.append(_issue(
                    f"{tpath}.input", "target_missing_input", f"{node_id} 노드에 {input_name or '선택한'} 입력이 없습니다.",
                ))
                continue
            value = inputs[input_name]
            if is_link(value):
                issues.append(_issue(
                    f"{tpath}.input", "target_is_link",
                    f"{node_id} 노드의 {input_name} 입력은 다른 노드에 연결되어 있어 파라미터로 노출할 수 없습니다.",
                ))
                continue
            key = (node_id, input_name)
            if key in seen_targets:
                issues.append(_issue(
                    tpath, "target_duplicate", f"{node_id} 노드의 {input_name} 입력을 여러 파라미터가 함께 바꾸고 있습니다.",
                ))
                continue
            seen_targets[key] = i
            if type_ok:
                mismatch = _target_type_message(ptype, node, node_id, input_name, value, specs)
                if mismatch:
                    issues.append(_issue(tpath, "type_mismatch", mismatch))

    outputs = m["outputs"]
    if not outputs:
        issues.append(_issue("outputs", "outputs_empty", "결과를 받을 출력 노드를 하나 이상 골라야 합니다."))
    for k, node_id in enumerate(outputs):
        if not isinstance(graph.get(node_id), dict):
            issues.append(_issue(f"outputs[{k}]", "output_not_found", f"출력 노드로 고른 {node_id} 노드가 워크플로우에 없습니다."))
    return issues


def _target_type_message(
    ptype: str,
    node: Dict[str, Any],
    node_id: str,
    input_name: str,
    value: Any,
    specs: Dict[str, Dict[str, Dict[str, Any]]],
) -> Optional[str]:
    class_specs = specs.get(str(node.get("class_type") or ""))
    spec = class_specs.get(input_name) if isinstance(class_specs, dict) else None
    kind = spec.get("kind") if isinstance(spec, dict) else None
    if kind in _KIND_ACCEPTS:
        if ptype not in _KIND_ACCEPTS[kind]:
            return f"{node_id} 노드의 {input_name} 입력은 {_KIND_KO[kind]} 값을 받는데 파라미터 형식이 {_TYPE_KO[ptype]}입니다."
        return None
    if kind is not None:  # LINK / OTHER — 규격으로 가를 수 없다
        return None
    inferred = infer_type(value)
    accepts = _VALUE_ACCEPTS.get(inferred)
    if accepts is None:
        return None
    if ptype in accepts or (ptype == "integer" and inferred == "number" and _is_integer(value)):
        return None
    return f"{node_id} 노드의 {input_name} 입력에 적힌 값은 {_TYPE_KO[inferred]}인데 파라미터 형식이 {_TYPE_KO[ptype]}입니다."


# ─── 도구 스키마 ─────────────────────────────────────────────────
def _json_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def tool_input_schema(mapping: Any) -> Dict[str, Any]:
    """에이전트 도구의 입력 JSON Schema.

    기본값과 무작위 안내는 description 끝에 영어로 덧붙인다(스키마 ``default``
    키는 일부 제공자가 거부하므로 쓰지 않는다).
    """
    m = normalize_mapping(mapping)
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for p in m["params"]:
        name = p["name"]
        if not name or name in properties:
            continue
        ptype = p["type"] if p["type"] in PARAM_TYPES else "string"
        prop: Dict[str, Any] = {"type": ptype}
        hints: List[str] = []
        if p["randomize"] and ptype == "integer":
            hints.append("Leave empty to use a random value.")
        elif p["default"] is not None:
            hints.append(f"Default: {_json_literal(p['default'])}.")
        description = " ".join(part for part in [p["description"].strip(), *hints] if part)
        if description:
            prop["description"] = description
        if ptype in ("integer", "number"):
            if _is_number(p["minimum"]):
                prop["minimum"] = p["minimum"]
            if _is_number(p["maximum"]):
                prop["maximum"] = p["maximum"]
        if p["choices"] and ptype != "boolean":
            prop["enum"] = list(p["choices"])
        properties[name] = prop
        if p["required"]:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# ─── 인자 적용 ───────────────────────────────────────────────────
def _coerce(param: Dict[str, Any], value: Any) -> Any:
    name, ptype = param["name"], param["type"]
    if ptype == "string":
        if isinstance(value, str):
            out: Any = value
        elif _is_number(value):
            out = str(value)
        else:
            raise ArgumentError(f"Argument '{name}' must be a string.")
    elif ptype == "integer":
        if _is_integer(value):
            out = int(value)
        elif isinstance(value, str) and _INT_RE.match(value.strip()):
            out = int(value.strip())
        else:
            raise ArgumentError(f"Argument '{name}' must be an integer.")
    elif ptype == "number":
        if _is_number(value):
            out = value
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            try:
                out = int(text) if _INT_RE.match(text) else float(text)
            except ValueError:
                raise ArgumentError(f"Argument '{name}' must be a number.") from None
            if not _is_number(out):
                raise ArgumentError(f"Argument '{name}' must be a finite number.")
        else:
            raise ArgumentError(f"Argument '{name}' must be a number.")
    elif ptype == "boolean":
        if isinstance(value, bool):
            out = value
        elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
            out = value.strip().lower() == "true"
        elif isinstance(value, int) and value in (0, 1):
            out = bool(value)
        else:
            raise ArgumentError(f"Argument '{name}' must be a boolean (true or false).")
    else:
        raise ArgumentError(f"Argument '{name}' has an unsupported type in the tool configuration.")

    if ptype in ("integer", "number"):
        lo, hi = param["minimum"], param["maximum"]
        lo = lo if _is_number(lo) else None
        hi = hi if _is_number(hi) else None
        if (lo is not None and out < lo) or (hi is not None and out > hi):
            if lo is not None and hi is not None:
                raise ArgumentError(f"Argument '{name}' must be between {lo} and {hi}.")
            if lo is not None:
                raise ArgumentError(f"Argument '{name}' must be at least {lo}.")
            raise ArgumentError(f"Argument '{name}' must be at most {hi}.")
    choices = param["choices"]
    if choices and ptype != "boolean" and out not in choices:
        raise ArgumentError(f"Argument '{name}' must be one of: {_json_literal(choices)}.")
    return out


def _random_integer(param: Dict[str, Any], rng: Any) -> int:
    lo = param["minimum"] if _is_number(param["minimum"]) else 0
    hi = min(param["maximum"], RANDOM_MAX) if _is_number(param["maximum"]) else RANDOM_MAX
    lo, hi = math.ceil(lo), math.floor(hi)
    if hi < lo:
        hi = lo
    return int(rng.randint(lo, hi))


def apply_arguments(
    graph: Dict[str, Dict[str, Any]],
    mapping: Any,
    arguments: Optional[Dict[str, Any]],
    *,
    rng: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """도구 인자를 워크플로우 깊은 복사에 적용한다.

    인자를 비웠을 때(키 없음, null, 문자열 형이 아닌데 빈 문자열):
    ``randomize`` 정수 → 무작위, 기본값 있음 → 기본값, 필수 → 오류, 그 밖 → 워크플로우 값.

    Raises:
        ArgumentError: 모르는 인자, 형·범위·선택지 위반, 필수 인자 누락.
    """
    m = normalize_mapping(mapping)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ArgumentError("Arguments must be a JSON object.")
    names = [p["name"] for p in m["params"]]
    unknown = sorted(str(k) for k in arguments if k not in names)
    if unknown:
        allowed = ", ".join(names) if names else "none"
        raise ArgumentError(f"Unknown argument(s): {', '.join(unknown)}. Allowed arguments: {allowed}.")

    out = copy.deepcopy(graph)
    source = rng if rng is not None else random
    for p in m["params"]:
        name = p["name"]
        raw = arguments.get(name)
        empty = raw is None or (isinstance(raw, str) and raw == "" and p["type"] != "string")
        if empty:
            if p["randomize"] and p["type"] == "integer":
                value = _random_integer(p, source)
            elif p["default"] is not None:
                value = _coerce(p, p["default"])
            elif p["required"]:
                raise ArgumentError(f"Missing required argument: {name}.")
            else:
                continue
        else:
            value = _coerce(p, raw)
        for target in p["targets"]:
            node = out.get(target["node_id"])
            inputs = node.get("inputs") if isinstance(node, dict) else None
            if not isinstance(inputs, dict) or target["input"] not in inputs or is_link(inputs[target["input"]]):
                raise ArgumentError(
                    f"Argument '{name}' cannot be applied because the workflow input "
                    f"{target['node_id']}.{target['input']} is missing or linked; the tool configuration must be fixed."
                )
            inputs[target["input"]] = copy.deepcopy(value)
    return out


__all__ = [
    "ISSUE_CODES",
    "MAPPING_VERSION",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_PARAMS",
    "PARAM_NAME_PATTERN",
    "PARAM_TYPES",
    "apply_arguments",
    "normalize_mapping",
    "tool_input_schema",
    "validate_entry",
]
