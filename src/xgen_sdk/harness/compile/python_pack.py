"""Python 패키지 빌더 — transpile 산출물 디렉토리 → wheel / sdist / tarball.

`build` (PEP 517) 가 설치돼 있으면 wheel/sdist 빌드 가능. 미설치 시
`pip install build` 안내 + tarball 폴백.

사용:
    from xgen_harness.compile.python_compile import transpile_to_python, write_package
    from xgen_harness.compile.python_pack import build_wheel, build_sdist, pack_tarball

    tree = transpile_to_python(snapshot, package_name="plateer-xgen-wf-abc", ...)
    pkg_dir = write_package(tree, "./dist/plateer-xgen-wf-abc-0.1.0")

    wheel_path = build_wheel(pkg_dir, out_dir="./dist")
    sdist_path = build_sdist(pkg_dir, out_dir="./dist")
    tarball_path = pack_tarball(pkg_dir, out_dir="./dist")
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("harness.compile.python_pack")


class BuildError(RuntimeError):
    """패키지 빌드 실패."""


# ─────────────────────────────────────────────────────────
# wheel / sdist — PEP 517 `build` 모듈 활용
# ─────────────────────────────────────────────────────────


def build_wheel(
    pkg_dir: str | Path,
    *,
    out_dir: Optional[str | Path] = None,
    python: Optional[str] = None,
) -> Path:
    """transpile 디렉토리 → wheel (.whl) 빌드.

    Args:
        pkg_dir: transpile_to_python + write_package 산출물 디렉토리
            (pyproject.toml 포함).
        out_dir: wheel 산출 디렉토리 (default: pkg_dir/dist).
        python: 빌드용 Python 인터프리터 경로 (default: sys.executable).

    Returns:
        생성된 wheel 파일 Path.
    """
    return _build_via_pep517(pkg_dir, out_dir=out_dir, python=python, kind="wheel")


def build_sdist(
    pkg_dir: str | Path,
    *,
    out_dir: Optional[str | Path] = None,
    python: Optional[str] = None,
) -> Path:
    """transpile 디렉토리 → sdist (.tar.gz) 빌드 (PEP 517)."""
    return _build_via_pep517(pkg_dir, out_dir=out_dir, python=python, kind="sdist")


def _build_via_pep517(
    pkg_dir: str | Path,
    *,
    out_dir: Optional[str | Path],
    python: Optional[str],
    kind: str,
) -> Path:
    pkg = Path(pkg_dir).resolve()
    if not (pkg / "pyproject.toml").exists():
        raise BuildError(f"pyproject.toml not found in {pkg}")
    out = Path(out_dir).resolve() if out_dir else pkg / "dist"
    out.mkdir(parents=True, exist_ok=True)

    py = python or sys.executable

    # `build` 모듈 사용 가능 여부 확인
    check = subprocess.run(
        [py, "-c", "import build"],
        capture_output=True,
    )
    if check.returncode != 0:
        raise BuildError(
            "PEP 517 `build` 모듈 미설치. "
            f"`{py} -m pip install build` 박은 후 재시도."
        )

    flag = "--wheel" if kind == "wheel" else "--sdist"
    result = subprocess.run(
        [py, "-m", "build", flag, str(pkg), "--outdir", str(out)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BuildError(
            f"`python -m build {flag}` 실패 (returncode={result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    # 생성된 산출물 찾기
    ext = ".whl" if kind == "wheel" else ".tar.gz"
    artifacts = sorted(out.glob(f"*{ext}"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not artifacts:
        raise BuildError(f"빌드 후 {ext} 산출물을 찾을 수 없음 (out_dir={out})")
    return artifacts[0]


# ─────────────────────────────────────────────────────────
# tarball — `build` 미설치 환경 폴백
# ─────────────────────────────────────────────────────────


def pack_tarball(
    pkg_dir: str | Path,
    *,
    out_dir: Optional[str | Path] = None,
    name: Optional[str] = None,
) -> Path:
    """transpile 디렉토리 → 단순 tar.gz 압축.

    `build` 미설치 환경 폴백. 사용자는 `pip install ./pkg-dir/` 또는
    `pip install ./pkg-dir.tar.gz` 가능.

    Args:
        pkg_dir: transpile 산출물 디렉토리.
        out_dir: tar.gz 산출 디렉토리 (default: pkg_dir.parent).
        name: tar.gz 파일명 (default: pkg_dir 이름 + .tar.gz).

    Returns:
        생성된 tar.gz Path.
    """
    pkg = Path(pkg_dir).resolve()
    if not pkg.exists():
        raise BuildError(f"pkg_dir not found: {pkg}")
    out = Path(out_dir).resolve() if out_dir else pkg.parent
    out.mkdir(parents=True, exist_ok=True)

    arc_name = name or f"{pkg.name}.tar.gz"
    target = out / arc_name

    with tarfile.open(target, "w:gz") as tar:
        tar.add(pkg, arcname=pkg.name)

    return target


# ─────────────────────────────────────────────────────────
# 일괄 — transpile → write → build
# ─────────────────────────────────────────────────────────


def compile_and_pack(
    snapshot_dict: dict,
    *,
    package_name: str,
    package_version: str = "0.1.0",
    include_mcp: bool = True,
    harness_version_pin: Optional[str] = None,
    workflow_description: str = "",
    tool_definitions: Optional[list[dict]] = None,
    metadata: Optional[dict] = None,
    out_dir: str | Path,
    format: str = "wheel",
) -> Path:
    """transpile + write + build 일괄 처리.

    Args:
        snapshot_dict: WorkflowSnapshot dict 또는 HarnessConfig dict.
        package_name / package_version / include_mcp / harness_version_pin /
            workflow_description: transpile_to_python 인자.
        out_dir: 최종 산출물 디렉토리.
        format: "wheel" / "sdist" / "tarball" / "source"
            - "source" 면 디렉토리만 박고 압축 안 함.

    Returns:
        format 별 산출물 Path (wheel/sdist/tarball) 또는 디렉토리 Path (source).
    """
    from .python_compile import transpile_to_python, write_package

    tree = transpile_to_python(
        snapshot_dict,
        package_name=package_name,
        package_version=package_version,
        include_mcp=include_mcp,
        harness_version_pin=harness_version_pin,
        workflow_description=workflow_description,
        tool_definitions=tool_definitions,
        metadata=metadata,
    )
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pkg_dir = out / f"{package_name}-{package_version}"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    write_package(tree, pkg_dir)

    if format == "source":
        return pkg_dir
    if format == "wheel":
        return build_wheel(pkg_dir, out_dir=out)
    if format == "sdist":
        return build_sdist(pkg_dir, out_dir=out)
    if format == "tarball":
        return pack_tarball(pkg_dir, out_dir=out)
    raise ValueError(f"unknown format: {format!r} (wheel/sdist/tarball/source)")
