"""Versioned text inputs in evaluation forms."""
import re


def evaluation_fields(raw):
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("텍스트 입력 박스는 최대 100개까지 추가할 수 있습니다.")
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
        if field.get("type") != "text" or not isinstance(field.get("required"), bool):
            raise ValueError("텍스트 입력 박스와 필수 여부를 확인하세요.")
        cfg = field.get("config")
        if not isinstance(cfg, dict):
            raise ValueError("입력 박스 설정이 올바르지 않습니다.")
        limit = cfg.get("max_length", 4000)
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError("최대 글자 수는 1~100000 사이의 정수여야 합니다.")
        if not isinstance(cfg.get("placeholder", ""), str) or len(cfg.get("placeholder", "")) > 200:
            raise ValueError("입력 안내는 200자 이내로 적어 주세요.")
    return raw


def evaluation_value_errors(definitions, values, *, required=True):
    evaluation_fields(definitions)
    if values is None:
        values = {}
    if not isinstance(values, dict):
        return ["평가 입력값 형식이 올바르지 않습니다."]
    errors = []
    for field in definitions:
        value = values.get(field["id"])
        if value is not None and not isinstance(value, str):
            errors.append(f"{field['label']}: 텍스트를 입력하세요.")
        elif value is not None and len(value) > field["config"].get("max_length", 4000):
            errors.append(f"{field['label']}: 최대 글자 수를 초과했습니다.")
        elif required and field["required"] and not (value or "").strip():
            errors.append(field["label"])
    return errors


def section_on(options, key):
    sections = options.get("sections") if isinstance(options, dict) else None
    return sections.get(key, True) if isinstance(sections, dict) else True
