"""
설치된 갤러리 자동 발견 (단계 6).

컴파일된 wheel 은 pyproject.toml 에 다음을 선언한다::

    [project.entry-points."xgen_harness.galleries"]
    <gallery_name> = "<package>:manifest"

이 모듈은 ``importlib.metadata.entry_points`` 로 설치된 갤러리를 수집한다.
엔진/이식측/UI 가 이 함수만 호출하면 설치된 갤러리 카탈로그를 즉시 렌더할 수 있다.

PyPI/사내 인덱스/로컬 wheel 어느 채널로 설치됐든 동일하게 발견됨 — 3채널 불가지론.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Callable, Optional

logger = logging.getLogger("harness.compile.gallery")

ENTRY_POINT_GROUP = "xgen_harness.galleries"


@dataclass
class InstalledGallery:
    """설치된 갤러리 한 개의 메타.

    Fields:
        entry_point_name: entry_points 에 등록된 이름 (일반적으로 gallery_name).
        manifest: 갤러리의 `manifest()` 호출 결과 (name/version/external_inputs/description 등).
        module_name: manifest 제공 모듈 경로 (e.g. "xgen_gallery_foo").
        dist_name: pip 배포 이름 (e.g. "xgen-gallery-foo"). manifest 에 있으면 우선,
                   없으면 module_name 에서 파생. UI 가 재조합할 필요 없이 그대로 사용.
        package_name: 임포트 가능한 Python 패키지 이름 (e.g. "xgen_gallery_foo").
    """

    entry_point_name: str
    manifest: dict[str, Any]
    module_name: str
    dist_name: str = ""
    package_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def discover_galleries(*, on_error: Optional[Callable[[str, Exception], None]] = None) -> list[InstalledGallery]:
    """설치된 모든 갤러리 발견 → 메타 목록.

    각 entry_point 의 로드 실패는 건너뛰고 로그 — 하나가 깨져도 나머지 카탈로그는 살림.

    Args:
        on_error: 옵션 콜백 `(entry_point_name, exception)`. UI 가 "로드 실패 배지" 노출할 때.

    Returns:
        발견된 갤러리 목록. 중복 이름이 있으면 먼저 발견된 것을 유지.
    """
    try:
        from importlib import metadata as _md
    except ImportError:
        logger.warning("importlib.metadata 미가용 — 갤러리 discover 비활성화")
        return []

    # Python 3.10+ 에서 entry_points(group=...) 필터 API 사용.
    try:
        eps = _md.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:
        # 레거시 (3.9 이하) 호환 — selects group after retrieval.
        all_eps = _md.entry_points()
        eps = getattr(all_eps, "select", lambda **_: [])(group=ENTRY_POINT_GROUP) or \
              all_eps.get(ENTRY_POINT_GROUP, [])

    seen: set[str] = set()
    out: list[InstalledGallery] = []
    for ep in eps:
        if ep.name in seen:
            continue
        seen.add(ep.name)
        try:
            resolver = ep.load()
        except Exception as e:
            logger.warning("gallery entry_point load 실패 (%s): %s", ep.name, e)
            if on_error is not None:
                try:
                    on_error(ep.name, e)
                except Exception as cb_e:
                    logger.debug("gallery on_error callback raised (ignored): %s", cb_e)
            continue

        # manifest 는 dict 이거나 callable().
        manifest: dict[str, Any]
        if callable(resolver):
            try:
                manifest = resolver() or {}
            except Exception as e:
                logger.warning("gallery manifest() 호출 실패 (%s): %s", ep.name, e)
                if on_error is not None:
                    try:
                        on_error(ep.name, e)
                    except Exception as cb_e:
                        logger.debug("gallery on_error callback raised (ignored): %s", cb_e)
                continue
        elif isinstance(resolver, dict):
            manifest = resolver
        else:
            logger.warning("gallery entry_point 는 callable 또는 dict 여야 함 (%s)", ep.name)
            continue

        module_name = getattr(ep, "module", "") or getattr(ep, "value", "").split(":")[0]
        manifest_dict = manifest if isinstance(manifest, dict) else {}
        # manifest 가 확정값을 내려줬다면 우선 사용 — 엔진이 컴파일 시점 계산한 규약.
        # 없으면 module_name 에서 파생 (예: xgen_gallery_foo → xgen-gallery-foo).
        #
        # 보안 검증 (v0.11.24) — manifest 의 package_name / dist_name 은 외부 패키지가
        # 내려줄 수 있으므로 신뢰 불가. path traversal · 임의 import 경로 주입을 차단하기 위해
        # 다음 규칙을 만족할 때만 사용한다. 위반 시 manifest 값을 무시하고 module_name 에서
        # 파생된 안전한 값으로 폴백.
        #   - 문자열 타입
        #   - 길이 1 ~ 64
        #   - `[A-Za-z_][A-Za-z0-9_-]*` (package_name 은 언더스코어, dist_name 은 하이픈 허용)
        import re
        _PKG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
        _DIST_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")   # PEP 503 호환 범위
        raw_pkg = manifest_dict.get("package_name")
        raw_dist = manifest_dict.get("dist_name")
        safe_pkg = raw_pkg if isinstance(raw_pkg, str) and _PKG_RE.match(raw_pkg) else None
        safe_dist = raw_dist if isinstance(raw_dist, str) and _DIST_RE.match(raw_dist) else None
        if raw_pkg and not safe_pkg:
            logger.warning(
                "gallery %s: manifest.package_name %r rejected (보안 규칙 위반) — module_name 으로 폴백",
                ep.name, raw_pkg,
            )
        if raw_dist and not safe_dist:
            logger.warning(
                "gallery %s: manifest.dist_name %r rejected (보안 규칙 위반) — package_name 에서 파생",
                ep.name, raw_dist,
            )
        pkg_name = safe_pkg or module_name
        dist_name = safe_dist or (pkg_name.replace("_", "-") if pkg_name else "")
        out.append(InstalledGallery(
            entry_point_name=ep.name,
            manifest=manifest_dict,
            module_name=module_name,
            package_name=pkg_name,
            dist_name=dist_name,
        ))
    return out


def get_gallery(name: str) -> Optional[InstalledGallery]:
    """단일 갤러리 조회 — 이름으로 필터."""
    for g in discover_galleries():
        if g.entry_point_name == name or g.manifest.get("name") == name:
            return g
    return None
