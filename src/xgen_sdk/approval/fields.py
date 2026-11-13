"""Versioned, typed evaluation data and server-calculated values."""
import re
from datetime import date
from .formula import formula_trees, computed_values, finite_number


def evaluation_fields(raw):
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("데이터 항목은 최대 100개까지 추가할 수 있습니다.")
    seen = set()
    for field in raw:
        if not isinstance(field, dict):
            raise ValueError("입력 박스 형식이 올바르지 않습니다.")
        key = field.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key) or key in seen:
            raise ValueError("입력 박스 ID는 고유해야 합니다.")
        seen.add(key)
        label = field.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise ValueError("입력 박스 이름은 1~200자로 입력하세요.")
        if field.get("type") not in ("text", "number", "date", "select", "checkbox", "formula") or not isinstance(field.get("required"), bool):
            raise ValueError("항목 종류와 필수 여부를 확인하세요.")
        cfg = field.get("config")
        if not isinstance(cfg, dict):
            raise ValueError("입력 박스 설정이 올바르지 않습니다.")
        limit = cfg.get("max_length", 4000)
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError("최대 글자 수는 1~100000 사이의 정수여야 합니다.")
        if not isinstance(cfg.get("placeholder", ""), str) or len(cfg.get("placeholder", "")) > 200:
            raise ValueError("입력 안내는 200자 이내로 적어 주세요.")
        if field["type"] == "select":
            choices = cfg.get("choices")
            if not isinstance(choices, list) or not 1 <= len(choices) <= 100:
                raise ValueError("선택지는 1~100개로 설정하세요.")
            codes = set()
            for choice in choices:
                if not isinstance(choice, dict) or not isinstance(choice.get("code"), str) or not choice["code"].strip() or len(choice["code"]) > 100 or choice["code"] in codes or not isinstance(choice.get("label"), str) or not choice["label"].strip() or len(choice["label"]) > 200:
                    raise ValueError("선택지의 고유 코드와 이름을 확인하세요.")
                codes.add(choice["code"])
    formula_trees(raw)
    return raw


def evaluation_value_errors(definitions, values, *, required=True):
    evaluation_fields(definitions)
    if values is None:
        values = {}
    if not isinstance(values, dict):
        return ["평가 입력값 형식이 올바르지 않습니다."]
    values, formula_errors = computed_values(definitions, values)
    errors = []
    for field in definitions:
        value = values.get(field["id"])
        kind = field["type"]
        if field["id"] in formula_errors:
            errors.append(f"{field['label']}: {formula_errors[field['id']]}")
        elif kind == "checkbox" and type(value) is not bool:
            errors.append(f"{field['label']}: 체크 여부를 확인하세요.")
        elif value is None or value == "" or (isinstance(value, str) and not value.strip()):
            if required and field["required"]:
                errors.append(field["label"])
        elif kind in ("number", "formula") and not finite_number(value):
            errors.append(f"{field['label']}: 올바른 숫자를 입력하세요.")
        elif kind == "date":
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"{field['label']}: 올바른 날짜를 선택하세요.")
        elif kind == "select" and value not in [item["code"] for item in field["config"]["choices"]]:
            errors.append(f"{field['label']}: 등록된 선택지를 선택하세요.")
        elif kind == "text" and not isinstance(value, str):
            errors.append(f"{field['label']}: 텍스트를 입력하세요.")
        elif kind == "text" and len(value) > field["config"].get("max_length", 4000):
            errors.append(f"{field['label']}: 최대 글자 수를 초과했습니다.")
    return errors


def section_on(options, key):
    sections = options.get("sections") if isinstance(options, dict) else None
    return sections.get(key, True) if isinstance(sections, dict) else True
