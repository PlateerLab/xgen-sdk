"""
npm_pack — 하네스 워크플로우 → npm tarball (.tgz).

산출물 구조::

    xgen-harness-{name}-{version}.tgz
    ├── package.json
    │     {
    │       "name": "xgen-harness-{sanitized_name}",
    │       "version": "{gallery_version}",
    │       "bin": {"xgen-harness-{name}": "./bin/cli.js"},
    │       "dependencies": {
    │         "@plateer-xgen/harness-engine-node": "^0.28.0"
    │       }
    │     }
    ├── bin/cli.js   — `require("@plateer-xgen/harness-engine-node").serve(spec)` thin wrapper
    └── spec.json    — fully equivalent harness spec (npm_spec.HarnessSpec)

사용:
    pkg = build_npm_package(snapshot, out_dir=Path("./dist"))
    # → pkg.tarball_path
    # 외부에서 `npx -y xgen-harness-{name}` 로 실행 (engine-node 가 spec 따라 동작)

mcp-station 등록 패턴:
    server_type="node",
    server_command="npx",
    server_args=["-y", "xgen-harness-{name}"]

레거시 wheel 빌더 (compile/wheel.py) 와 병행 — 새 publish 는 모두 이쪽으로.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .npm_spec import HarnessSpec, build_spec, FrozenToolDefinition
from .snapshot import WorkflowSnapshot


# npm 패키지 이름 — unscoped (npmjs 글로벌 namespace).
# 외부 호환 우선 — Claude Desktop / Cursor / 외부 mcp-station 어디서나 동일 npx.
# unscoped — npm org 등록 진입장벽 회피.
NPM_PACKAGE_PREFIX = "xgen-harness-"

# bin 이름 prefix.
BIN_NAME_PREFIX = "xgen-harness-"

# engine-node 의존성 범위. wrapper tarball 안의 package.json 에 박혀, npm 이
# npmjs registry 에서 자동 다운로드. **이게 라운드트립의 키** — wrapper 는 minio
# presigned tarball, engine-node 는 npmjs 에서.
# alpha tag 단계: ^0.28.0 은 prerelease 안 받으니 정확 매칭. stable 시 ^ 로.
DEFAULT_ENGINE_DEP = "^0.31.6"
ENGINE_PACKAGE = "@plateer-xgen/harness-engine-node"


_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass
class NpmPackResult:
    """npm pack 결과.

    legacy WheelBuildResult 와 호환되도록 ``dist_name`` / ``wheel_path`` alias
    노출 — 기존 publisher 시그니처 (``wheel_path=Path``) 변경 없이 npm 채널 사용.
    """

    package_name: str          # xgen-harness-foo
    version: str
    tarball_path: Path         # .tgz 파일 경로
    skeleton_dir: Path         # 빌드 디렉토리 (tarball 추출 전)
    package_json: dict[str, Any]
    spec_path: Path
    cli_path: Path
    size_bytes: int

    @property
    def dist_name(self) -> str:
        """legacy 호환 — `xgen-harness-foo` 형태 (npm safe name)."""
        # xgen-harness-foo → xgen-harness-foo
        return self.package_name.replace("@", "").replace("/", "-")

    @property
    def wheel_path(self) -> Path:
        """legacy alias — wheel 호출처가 그대로 동작하도록 tarball_path 노출."""
        return self.tarball_path


def build_npm_package(
    snapshot: WorkflowSnapshot,
    *,
    out_dir: Path,
    tool_definitions: Optional[list[FrozenToolDefinition]] = None,
    engine_dep_range: str = DEFAULT_ENGINE_DEP,
    extra_metadata: Optional[dict[str, Any]] = None,
    keep_skeleton: bool = False,
) -> NpmPackResult:
    """``WorkflowSnapshot`` → npm tarball.

    내부 흐름:
      1. spec = build_spec(snapshot, tool_definitions, ...)
      2. 임시 skeleton 디렉토리:
         package.json + bin/cli.js + spec.json
      3. ``npm pack`` (또는 fallback: tar gz)
      4. tarball 을 out_dir 로 이동

    npm CLI 가 없는 환경 (예: 사내 cdn 빌더 컨테이너) 대비 ``tar`` 폴백.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = build_spec(snapshot, tool_definitions=tool_definitions, extra_metadata=extra_metadata)

    sanitized = _sanitize_name(snapshot.gallery_name)
    package_name = f"{NPM_PACKAGE_PREFIX}{sanitized}"
    bin_name = f"{BIN_NAME_PREFIX}{sanitized}"
    version = snapshot.gallery_version or "0.1.0"

    skeleton = Path(tempfile.mkdtemp(prefix=f"xgen-harness-npm-{sanitized}-"))
    try:
        # bin/cli.js
        bin_dir = skeleton / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        cli_path = bin_dir / "cli.js"
        cli_path.write_text(_render_cli_js(), encoding="utf-8")
        # 실행 가능 비트 (tar 안에서도 유지됨)
        os.chmod(cli_path, 0o755)

        # spec.json
        spec_path = skeleton / "spec.json"
        spec_path.write_text(spec.to_json(), encoding="utf-8")

        # package.json
        pkg_json = _render_package_json(
            package_name=package_name,
            bin_name=bin_name,
            version=version,
            description=snapshot.metadata.get("description")
                or f"Harness workflow {snapshot.gallery_name}",
            engine_dep_range=engine_dep_range,
        )
        pkg_path = skeleton / "package.json"
        pkg_path.write_text(
            json.dumps(pkg_json, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # README.md (optional, light)
        # spec.tool_definitions 가 박혀 있으면 노드별 시크릿 자리도 ENV 안내에
        # 포함되도록 spec 객체째 전달. snapshot 만으론 tool_definitions 가 안 보임.
        readme = skeleton / "README.md"
        readme.write_text(
            _render_readme(snapshot, package_name, bin_name, spec=spec),
            encoding="utf-8",
        )

        # npm pack — npm 이 있으면 사용, 없으면 tar gz fallback
        tarball = _run_npm_pack_or_fallback(skeleton, out_dir, package_name, version)

        return NpmPackResult(
            package_name=package_name,
            version=version,
            tarball_path=tarball,
            skeleton_dir=skeleton,
            package_json=pkg_json,
            spec_path=spec_path,
            cli_path=cli_path,
            size_bytes=tarball.stat().st_size if tarball.exists() else 0,
        )
    finally:
        if not keep_skeleton:
            try:
                shutil.rmtree(skeleton, ignore_errors=True)
            except Exception:
                pass


def compile_workflow_to_npm(
    *,
    harness_config: Any,
    workflow_data: Optional[dict[str, Any]] = None,
    gallery_name: str,
    gallery_version: str = "0.1.0",
    out_dir: Path,
    tool_definitions: Optional[list[FrozenToolDefinition]] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
    engine_dep_range: str = DEFAULT_ENGINE_DEP,
) -> NpmPackResult:
    """One-shot — HarnessConfig → snapshot → npm tarball.

    ``compile_workflow`` (legacy wheel) 와 동일 인터페이스 — wheel 대신 npm.
    publish 흐름에서 이 함수만 부르면 끝.
    """
    snapshot = WorkflowSnapshot.from_config(
        harness_config=harness_config,
        workflow_data=workflow_data,
        gallery_name=gallery_name,
        gallery_version=gallery_version,
    )
    return build_npm_package(
        snapshot,
        out_dir=Path(out_dir),
        tool_definitions=tool_definitions,
        engine_dep_range=engine_dep_range,
        extra_metadata=extra_metadata,
    )


# ─── 내부 helpers ──────────────────────────────────────────────────


def _sanitize_name(name: str) -> str:
    """npm package name 호환 sanitize (lowercase, hyphen/underscore only)."""
    s = (name or "").lower().strip()
    s = _NAME_SANITIZE_RE.sub("-", s)
    s = s.strip("-_") or "harness"
    return s[:200]


def _render_cli_js() -> str:
    """bin/cli.js — engine-node 의 serve_mcp 한 줄 호출."""
    return """#!/usr/bin/env node
/**
 * Auto-generated by xgen-harness compile/npm_pack.py
 * 이 파일을 직접 수정하지 마세요. Workflow 변경 시 재컴파일.
 *
 * @plateer-xgen/harness-engine-node 가 spec.json 을 읽어 13 stage / 31 strategy /
 * 모든 stage_params 를 fully equivalent 로 실행. 이 파일은 thin wrapper.
 */
"use strict";

const path = require("path");
const fs = require("fs");

const specPath = path.join(__dirname, "..", "spec.json");
if (!fs.existsSync(specPath)) {
  console.error("[xgen-harness] spec.json not found at", specPath);
  process.exit(2);
}

let spec;
try {
  spec = JSON.parse(fs.readFileSync(specPath, "utf-8"));
} catch (e) {
  console.error("[xgen-harness] spec.json parse failed:", e.message || e);
  process.exit(3);
}

let engine;
try {
  engine = require("@plateer-xgen/harness-engine-node");
} catch (e) {
  console.error(
    "[xgen-harness] @plateer-xgen/harness-engine-node not installed. " +
    "Run `npm install @plateer-xgen/harness-engine-node` first."
  );
  console.error(e.message || e);
  process.exit(4);
}

const cmd = process.argv[2] || "serve-mcp";

if (cmd === "serve-mcp") {
  // stdio MCP 서버. mcp-station / Claude Desktop / Cursor 가 spawn.
  engine.serveMcp(spec).catch((err) => {
    console.error("[xgen-harness] serve-mcp failed:", err.stack || err);
    process.exit(1);
  });
} else if (cmd === "run") {
  // 단발 실행 — input 을 stdin 으로 받아 출력 stdout.
  const input = process.argv.slice(3).join(" ");
  engine.runOnce(spec, input).then((out) => {
    process.stdout.write(JSON.stringify(out) + "\\n");
  }).catch((err) => {
    console.error("[xgen-harness] run failed:", err.stack || err);
    process.exit(1);
  });
} else if (cmd === "version" || cmd === "--version" || cmd === "-v") {
  console.log(spec.gallery_name, spec.gallery_version);
} else {
  console.error("[xgen-harness] unknown command:", cmd);
  console.error("usage: xgen-harness-<name> [serve-mcp|run|version]");
  process.exit(2);
}
"""


def _render_package_json(
    *,
    package_name: str,
    bin_name: str,
    version: str,
    description: str,
    engine_dep_range: str,
) -> dict[str, Any]:
    """package.json — engine-node 만 의존, files 에 spec.json 명시."""
    return {
        "name": package_name,
        "version": version,
        "description": description,
        "type": "commonjs",
        "main": "bin/cli.js",
        "bin": {bin_name: "bin/cli.js"},
        "files": [
            "bin/",
            "spec.json",
            "README.md",
        ],
        "engines": {
            "node": ">=18.0.0",
        },
        "dependencies": {
            ENGINE_PACKAGE: engine_dep_range,
        },
        "keywords": [
            "xgen", "harness", "mcp", "agent", "llm",
        ],
        "license": "UNLICENSED",
        "private": False,
    }


def _render_readme(
    snapshot: WorkflowSnapshot,
    package_name: str,
    bin_name: str,
    *,
    spec: Any = None,
) -> str:
    from ._env_hints import render_required_envs_markdown
    # snapshot 의 harness_config dict 를 보고 required env 추론.
    # WorkflowSnapshot.to_dict() 또는 직접 노출된 dict 접근 — 안전하게 폴백.
    try:
        snap_dict = snapshot.to_dict() if hasattr(snapshot, "to_dict") else {}
        config_for_env = snap_dict.get("harness_config") or snap_dict.get("config") or {}
    except Exception:
        config_for_env = {}
    # spec 이 있으면 tool_definitions 도 ENV 안내에 포함 (노드 시크릿 자리).
    tool_defs: list[dict[str, Any]] | None = None
    if spec is not None:
        try:
            spec_dict = spec.to_dict() if hasattr(spec, "to_dict") else dict(spec)
            tool_defs = spec_dict.get("tool_definitions") or []
            if not isinstance(tool_defs, list):
                tool_defs = []
        except Exception:
            tool_defs = None
    env_section = render_required_envs_markdown(
        config_for_env, header_level=2, tool_definitions=tool_defs,
    )
    return f"""# {package_name}

`{snapshot.gallery_name}` v{snapshot.gallery_version} — auto-generated harness MCP server.

{env_section}
## Run as MCP server

```bash
npx -y {package_name}
```

## mcp-station / Claude Desktop config

```json
{{
  "mcpServers": {{
    "{snapshot.gallery_name}": {{
      "command": "npx",
      "args": ["-y", "{package_name}"]
    }}
  }}
}}
```

## What is inside

- All harness stage settings frozen in `spec.json` (fully equivalent to the
  original workflow — no missing strategies, no minimal pipeline).
- Engine: `@plateer-xgen/harness-engine-node` reads the spec and runs the same 13-stage
  pipeline as the original `xgen-harness` Python engine.
"""


def _run_npm_pack_or_fallback(
    skeleton: Path, out_dir: Path, package_name: str, version: str,
) -> Path:
    """npm 이 있으면 ``npm pack`` 사용, 없으면 직접 tar gz."""
    npm = shutil.which("npm")
    if npm:
        try:
            r = subprocess.run(
                [npm, "pack", "--pack-destination", str(out_dir), "--silent"],
                cwd=str(skeleton),
                check=True,
                capture_output=True,
                timeout=120,
            )
            # npm pack stdout 마지막 줄 = 만들어진 파일 이름
            out_name = (r.stdout.decode().strip().splitlines() or [""])[-1].strip()
            if out_name:
                p = out_dir / out_name
                if p.exists():
                    return p
        except Exception:
            pass  # fallback 으로 진행

    # fallback: 직접 tar gz
    safe = package_name.replace("@", "").replace("/", "-")
    tarball = out_dir / f"{safe}-{version}.tgz"
    # npm pack 은 skeleton 내부를 'package/' 디렉토리로 wrap. 표준 npm tarball.
    import tarfile
    with tarfile.open(tarball, "w:gz") as tf:
        for path in skeleton.rglob("*"):
            if path.is_file():
                rel = path.relative_to(skeleton)
                arcname = "package/" + str(rel).replace(os.sep, "/")
                tf.add(str(path), arcname=arcname)
    return tarball
