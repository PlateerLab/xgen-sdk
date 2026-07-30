"""SF6 — agentskills.io interop: 원장 스킬 <-> `SKILL.md` 패키지.

**왜 이게 전략적으로 중요한가.** Agent Skills 는 앤트로픽이 만들어 공개 표준으로
풀었고 Claude Code·Cursor·Copilot·VS Code·Gemini CLI·Codex·OpenHands·Goose·Letta
등 수십 개 제품이 채택했다. 그런데 표준이 정의한 검사는 `skills-ref validate` —
**프론트매터 문법과 이름 규칙만** 본다. "이 스킬이 실제로 효과가 있나"는 표준에
없다. Hermes 도 자동 생성만 하고 검증은 없다.

그래서 이 모듈의 역할은 두 방향이다.
- **내보내기**: 홀드아웃 게이트를 통과한 스킬을 스펙 호환 `SKILL.md` 로 내보낸다.
  → 검증된 스킬이 45개 제품 어디서나 쓰인다. 증거는 스펙이 허용한 자유 필드
  `metadata` 에 실어 보내므로 표준을 깨지 않으면서 출처·이득이 따라간다.
- **들여오기**: 남이 만든 `SKILL.md` 를 후보로 받아 재현벤치에 태운다.
  → 생태계의 어떤 스킬이든 "효과가 실측된 것"과 아닌 것을 가를 수 있다.
  아무도 안 하는 일이고, 우리 게이트가 이미 그걸 한다.

스펙 준수는 타협 대상이 아니다 — 규칙을 어긴 프론트매터는 다른 에이전트가 조용히
무시하거나 거부한다. 그래서 내보내기 전에 스스로 검증하고, 위반이면 예외를 던진다.

스펙 출처: https://agentskills.io/specification (2026-07 확인)
"""

from __future__ import annotations

import json
import re
from typing import Any

from .model import Provenance, SkillCandidate, SkillDef

# 스펙 상한 — 넘으면 다른 클라이언트가 거부한다.
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500

# name: 소문자 영숫자와 하이픈, 앞뒤 하이픈 금지, 연속 하이픈 금지.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# metadata 키는 충돌을 피하라고 스펙이 권고한다 — 우리 것은 전부 이 접두를 쓴다.
META_PREFIX = "xgen-jermes-"


def validate_name(name: str) -> list[str]:
    problems: list[str] = []
    if not name:
        problems.append("name 이 비어 있다")
        return problems
    if len(name) > MAX_NAME:
        problems.append(f"name 이 {MAX_NAME}자를 넘는다({len(name)})")
    if not _NAME_RE.match(name):
        problems.append(
            "name 은 소문자 영숫자와 하이픈만 쓰고, 앞뒤 하이픈·연속 하이픈이 없어야 한다"
            f" (받은 값: {name!r})")
    return problems


def validate_description(description: str) -> list[str]:
    problems: list[str] = []
    text = (description or "").strip()
    if not text:
        problems.append("description 이 비어 있다(스펙상 필수·비어있으면 안 됨)")
    if len(text) > MAX_DESCRIPTION:
        problems.append(f"description 이 {MAX_DESCRIPTION}자를 넘는다({len(text)})")
    return problems


def spec_name(raw: str) -> str:
    """임의 문자열을 스펙에 맞는 name 으로 접는다.

    `_sanitize_name` 이 64자로 자르면서 하이픈으로 끝나 스펙을 어길 수 있었다 —
    자른 다음에 다시 깎아야 한다(순서가 중요).
    """
    name = re.sub(r"[^a-z0-9-]", "-", raw.strip().lower().replace("_", "-"))
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME].rstrip("-")   # 자른 뒤 재정리
    return name or "unnamed-skill"


def _yaml_scalar(value: str) -> str:
    """의존성 없이 YAML 스칼라를 안전하게 쓴다.

    콜론·해시·따옴표·개행이 들어간 description 을 날값으로 적으면 프론트매터가
    깨져 스킬이 통째로 무시된다. 여러 줄은 한 줄로 접는다(프론트매터는 단행이
    안전하고, 본문에 이미 전체 서술이 있다).
    """
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return '""'
    if re.search(r'[:#\-\[\]{}&*!|>%@`"\']', text) or text[0] in "?,":
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _yaml_string(value: Any) -> str:
    """항상 따옴표로 감싼 YAML 문자열 — metadata 값 전용."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _body_without_frontmatter(body: str) -> str:
    """우리 내부 body 에 붙어 있던 예전 프론트매터를 떼어낸다.

    안 떼면 내보낸 파일에 프론트매터가 두 번 들어가고, 두 번째 것은 본문
    텍스트로 읽혀 프롬프트를 오염시킨다."""
    text = (body or "").lstrip()
    if not text.startswith("---"):
        return body or ""
    end = text.find("\n---", 3)
    if end == -1:
        return body or ""
    return text[end + 4:].lstrip("\n")


def to_skill_md(skill: SkillDef, *, evidence: dict[str, Any] | None = None,
                license_name: str = "") -> str:
    """원장 스킬 -> 스펙 호환 `SKILL.md` 텍스트.

    검증 증거는 `metadata` 로 간다 — 스펙이 정의하지 않은 필드를 최상위에 쓰면
    호환이 깨지지만, `metadata` 는 자유 키/값 맵이라 표준을 지키면서 실을 수 있다.
    """
    problems = validate_name(skill.name) + validate_description(skill.description)
    if problems:
        raise ValueError("스펙 위반으로 내보낼 수 없다: " + "; ".join(problems))

    meta: dict[str, str] = {
        f"{META_PREFIX}kind": skill.kind,
        f"{META_PREFIX}scope": skill.scope,
        f"{META_PREFIX}version": skill.version,
        f"{META_PREFIX}status": skill.status,
        # 이 표준에 없는 단 하나의 정보 = 효과가 실측됐는지.
        f"{META_PREFIX}verified": "true" if skill.verified else "false",
    }
    if skill.supersedes:
        meta[f"{META_PREFIX}supersedes"] = skill.supersedes
    if skill.provenance:
        prov = skill.provenance
        if getattr(prov, "origin", ""):
            meta[f"{META_PREFIX}origin"] = prov.origin
        runs = list(getattr(prov, "source_run_ids", []) or [])
        if runs:
            meta[f"{META_PREFIX}source-runs"] = ",".join(runs[:8])
        if getattr(prov, "signal", ""):
            meta[f"{META_PREFIX}signal"] = prov.signal
    for key, value in (evidence or {}).items():
        meta[f"{META_PREFIX}{key}"] = str(value)

    lines = ["---", f"name: {skill.name}",
             f"description: {_yaml_scalar(skill.description)}"]
    if license_name:
        lines.append(f"license: {_yaml_scalar(license_name)}")
    lines.append("metadata:")
    for key in sorted(meta):
        # 스펙: metadata 는 "문자열 키 -> 문자열 값" 맵. 날값으로 적으면 true/0.34 가
        # 불리언·숫자로 파싱돼 타입 계약이 깨진다 — 항상 따옴표로 감싼다.
        lines.append(f"  {key}: {_yaml_string(meta[key])}")
    lines.append("---")

    body = _body_without_frontmatter(skill.body).rstrip()
    if skill.kind != "guide":
        # config/tool 은 마크다운이 아니라 JSON/매니페스트다. 그대로 흘리면 다른
        # 에이전트가 지시문으로 읽으므로 코드블록으로 감싸 설명을 붙인다.
        body = (f"# {skill.name}\n\n{skill.description}\n\n"
                f"## Payload ({skill.kind})\n\n```json\n{body}\n```\n")
    elif not body:
        body = f"# {skill.name}\n\n{skill.description}\n"
    return "\n".join(lines) + "\n\n" + body + "\n"


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """`SKILL.md` -> (프론트매터 dict, 본문). 의존성 0으로 필요한 만큼만 읽는다.

    중첩은 `metadata:` 한 단계만 지원한다 — 스펙이 정의한 구조가 그것뿐이다.
    """
    raw = (text or "").lstrip()
    if not raw.startswith("---"):
        raise ValueError("프론트매터가 없다(`---` 로 시작해야 한다)")
    end = raw.find("\n---", 3)
    if end == -1:
        raise ValueError("프론트매터가 닫히지 않았다")
    head, body = raw[3:end], raw[end + 4:].lstrip("\n")

    front: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        if ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        key, value = key.strip(), value.strip()
        if indented and current is not None:
            current[key] = _unquote(value)
            continue
        if not value:
            current = {}
            front[key] = current
            continue
        current = None
        front[key] = _unquote(value)
    return front, body


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return text


def validate_skill_md(text: str) -> list[str]:
    """스펙 위반 목록. 빈 리스트면 다른 에이전트가 받아준다는 뜻."""
    try:
        front, _ = parse_skill_md(text)
    except ValueError as exc:
        return [str(exc)]
    problems = validate_name(str(front.get("name", "")))
    problems += validate_description(str(front.get("description", "")))
    compatibility = str(front.get("compatibility", "") or "")
    if len(compatibility) > MAX_COMPATIBILITY:
        problems.append(f"compatibility 가 {MAX_COMPATIBILITY}자를 넘는다")
    metadata = front.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        problems.append("metadata 는 키/값 맵이어야 한다")
    return problems


def candidate_from_skill_md(text: str, *, scope: str = "user",
                            curator_id: str = "skill-md-import") -> SkillCandidate:
    """남의 `SKILL.md` 를 우리 후보로 들여온다 — 게이트에 태우기 위한 입구.

    들여올 때 `verified` 는 절대 믿지 않는다. 남이 스스로 붙인 표시이고,
    검증은 우리 벤치가 이 환경에서 다시 해야 의미가 있다(그게 차별점이다).
    """
    problems = validate_skill_md(text)
    if problems:
        raise ValueError("들여올 수 없는 SKILL.md: " + "; ".join(problems))
    front, body = parse_skill_md(text)
    metadata = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}
    return SkillCandidate(
        name=spec_name(str(front["name"])),
        kind="guide",
        scope=scope,
        action="create",
        rationale=str(front.get("description", ""))[:500],
        when_to_use=str(front.get("description", ""))[:300],
        procedure=_body_steps(body) or ["(본문 참조)"],
        verification=[],
        payload={"imported_body": body,
                 "imported_metadata": dict(metadata),
                 "claimed_verified": str(metadata.get(f"{META_PREFIX}verified", "")),
                 "license": str(front.get("license", ""))},
        provenance=Provenance(origin="skill_md_import", curator_id=curator_id),
    )


def _body_steps(body: str) -> list[str]:
    """본문에서 절차로 보이는 줄만 뽑는다(불릿·번호). 없으면 빈 목록."""
    steps: list[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", stripped):
            steps.append(re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", stripped)[:300])
        if len(steps) >= 12:
            break
    return steps


def skill_package(skill: SkillDef, *, evidence: dict[str, Any] | None = None,
                  license_name: str = "") -> dict[str, str]:
    """{상대경로: 내용} — 파일시스템에 쓰든 zip 으로 묶든 호출측이 결정한다.

    디렉토리 이름은 `name` 과 같아야 한다는 스펙 규칙을 여기서 지킨다.
    """
    text = to_skill_md(skill, evidence=evidence, license_name=license_name)
    files = {f"{skill.name}/SKILL.md": text}
    if skill.kind != "guide":
        # 원본 payload 도 같이 실어야 기계가 다시 쓸 수 있다(본문 코드블록은 사람용).
        files[f"{skill.name}/assets/payload.json"] = _body_without_frontmatter(
            skill.body).strip() or json.dumps({})
    return files
