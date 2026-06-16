"""
Sandbox — subprocess 격리 실행기 (v0.16.0).

용도:
  1. **Tool Synthesis** — LLM 이 생성한 파이썬 코드를 엔진 프로세스에 직접 exec 하는 것은
     보안 리스크. 서브프로세스로 격리해 timeout / stdout size cap / cwd 격리.
  2. **갤러리 휠 테스트** — 컴파일된 wheel 을 임시 venv 에 설치·실행.
  3. **NOMNode 독립 실행** — 노드 하나를 격리 환경에서 시연.

디자인 원칙:
  - 외부 의존성 0 (subprocess / tempfile / json 만). Docker 의존 안 한다.
  - UNIX/Linux 우선. Windows 는 best-effort.
  - 진짜 보안 격리(시스템콜 차단) 는 Phase 3.5 에서 firejail/gvisor 연동.
    지금은 "프로세스 격리 + 리소스 상한 + 타임아웃" 3 지점만 확실히 제공.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("harness.sandbox")


@dataclass
class SandboxLimits:
    """POSIX rlimit 기반 per-sandbox 리소스 상한 (v0.16.1).

    xgen-sandbox 의 `resources` 필드 아이디어 차용 — per-call 단위로 CPU/메모리/
    파일·열린파일 상한을 통합 스펙으로. 진짜 격리(cgroups/seccomp)는 Phase 4+ 에서.
    지금은 POSIX `resource.setrlimit` 로 best-effort.

    Attributes
    ----------
    cpu_seconds : int
        RLIMIT_CPU. child 가 사용할 수 있는 CPU time(초). 초과 시 SIGKILL.
    address_space_mb : int
        RLIMIT_AS. 가상 주소 공간(MB). malloc 폭주 차단.
    max_open_files : int
        RLIMIT_NOFILE. 동시에 열 수 있는 파일/소켓 수.
    max_file_size_mb : int
        RLIMIT_FSIZE. 쓰기 가능한 단일 파일 크기(MB).
    no_core_dump : bool
        RLIMIT_CORE=0 으로 core dump 비활성.
    """
    cpu_seconds: int = 5
    address_space_mb: int = 512
    max_open_files: int = 64
    max_file_size_mb: int = 16
    no_core_dump: bool = True


DEFAULT_LIMITS = SandboxLimits()


@dataclass
class SandboxResult:
    success: bool
    stdout: str
    stderr: str
    return_value: object = None
    exit_code: int = 0
    timed_out: bool = False
    duration_ms: int = 0
    applied_limits: Optional[dict] = None


class Sandbox:
    """단일 파이썬 스니펫을 subprocess 로 격리 실행.

    Usage:
        result = Sandbox(timeout_sec=5, max_output_bytes=65536).run_code(
            '''
            import json, sys
            data = json.loads(sys.stdin.read())
            print(json.dumps({"echo": data["msg"].upper()}))
            ''',
            stdin_payload={"msg": "hello"},
        )
        assert result.success
        assert result.return_value == {"echo": "HELLO"}
    """

    def __init__(
        self,
        *,
        timeout_sec: float = 5.0,
        max_output_bytes: int = 1_048_576,
        python_executable: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        limits: Optional[SandboxLimits] = None,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.max_output_bytes = max_output_bytes
        self.python_executable = python_executable or sys.executable
        self.env = env
        self.cwd = cwd
        self.limits = limits or DEFAULT_LIMITS

    def _build_preexec_fn(self):
        """POSIX rlimit 적용 preexec_fn. Windows 는 None 반환 (setrlimit 없음).

        xgen-sandbox 의 resources 필드 아이디어 차용 — 하지만 우리는 K8s 대신
        resource.setrlimit 로 best-effort. 부모 영향 없이 child 만 제한.
        """
        if os.name != "posix":
            return None

        limits = self.limits
        mb = 1024 * 1024

        def _apply():  # pragma: no cover — child process 에서만 실행
            try:
                import resource
                # CPU time (soft=hard)
                if limits.cpu_seconds > 0:
                    resource.setrlimit(
                        resource.RLIMIT_CPU,
                        (limits.cpu_seconds, limits.cpu_seconds),
                    )
                # Address space (가상메모리 상한)
                if limits.address_space_mb > 0:
                    b = limits.address_space_mb * mb
                    resource.setrlimit(resource.RLIMIT_AS, (b, b))
                # 열린 파일 수
                if limits.max_open_files > 0:
                    n = limits.max_open_files
                    resource.setrlimit(resource.RLIMIT_NOFILE, (n, n))
                # 단일 파일 쓰기 크기
                if limits.max_file_size_mb > 0:
                    b = limits.max_file_size_mb * mb
                    resource.setrlimit(resource.RLIMIT_FSIZE, (b, b))
                # Core dump
                if limits.no_core_dump:
                    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            except Exception:
                # rlimit 실패해도 실행은 계속 (best-effort) — Docker 등 제한 환경 대응.
                pass

        return _apply

    def run_code(
        self,
        code: str,
        *,
        stdin_payload: Optional[object] = None,
    ) -> SandboxResult:
        """code 를 subprocess 로 실행. stdout 마지막 줄을 JSON 으로 파싱해 return_value 로.

        code 는 stdin 에서 JSON 읽고 stdout 으로 JSON 한 줄 쓰는 것이 규약.
        (규약을 못 지킨 코드여도 success/stdout/stderr 은 받을 수 있음.)
        """
        import time

        stdin_str = ""
        if stdin_payload is not None:
            try:
                stdin_str = json.dumps(stdin_payload, ensure_ascii=False)
            except TypeError:
                stdin_str = json.dumps(str(stdin_payload))

        # 임시 파일에 코드 저장 — -c 인자가 너무 길면 플랫폼별로 잘림.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            code_path = f.name

        start = time.time()
        timed_out = False
        preexec = self._build_preexec_fn()
        try:
            proc = subprocess.run(
                [self.python_executable, "-I", code_path],
                input=stdin_str,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                env=self.env,
                cwd=self.cwd,
                preexec_fn=preexec,
            )
            stdout = proc.stdout[: self.max_output_bytes]
            stderr = proc.stderr[: self.max_output_bytes]
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            stdout = (e.stdout or "")[: self.max_output_bytes] if isinstance(e.stdout, str) \
                else ((e.stdout or b"").decode("utf-8", "replace")[: self.max_output_bytes])
            stderr = (e.stderr or "")[: self.max_output_bytes] if isinstance(e.stderr, str) \
                else ((e.stderr or b"").decode("utf-8", "replace")[: self.max_output_bytes])
            exit_code = -1
        finally:
            try:
                os.unlink(code_path)
            except OSError:
                pass

        duration_ms = int((time.time() - start) * 1000)

        # 마지막 비어있지 않은 줄을 return_value JSON 으로 해석 시도.
        return_value = None
        for line in reversed((stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return_value = json.loads(line)
            except Exception:
                pass
            break

        return SandboxResult(
            success=(not timed_out and exit_code == 0),
            stdout=stdout or "",
            stderr=stderr or "",
            return_value=return_value,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            applied_limits={
                "cpu_seconds": self.limits.cpu_seconds,
                "address_space_mb": self.limits.address_space_mb,
                "max_open_files": self.limits.max_open_files,
                "max_file_size_mb": self.limits.max_file_size_mb,
                "posix": os.name == "posix",
            },
        )

    def run_nom_tool(
        self,
        *,
        entry: str,
        input_payload: dict,
    ) -> SandboxResult:
        """NOMNode.entry ("module:callable") 를 격리 실행.

        code 는 규약에 따라 stdin 에서 JSON 을 받아 `result = callable(**input)` 호출,
        stdout 에 `json.dumps(result)` 한 줄. 실패 시 stderr.
        """
        module_name, _, call_name = entry.partition(":")
        if not module_name or not call_name:
            return SandboxResult(success=False, stdout="", stderr=f"invalid entry: {entry}")

        wrapper = f"""
import json, sys, importlib, traceback
try:
    mod = importlib.import_module({module_name!r})
    fn = getattr(mod, {call_name!r})
    payload = json.loads(sys.stdin.read() or "{{}}")
    result = fn(**payload) if callable(fn) else fn
    print(json.dumps(result, default=str))
except Exception as e:
    sys.stderr.write(traceback.format_exc())
    sys.exit(1)
"""
        return self.run_code(wrapper, stdin_payload=input_payload)


# ────────────────────────────────────────────────────────────────
#  편의 함수
# ────────────────────────────────────────────────────────────────

def run_sandboxed(
    code: str,
    *,
    stdin_payload: Optional[object] = None,
    timeout_sec: float = 5.0,
) -> SandboxResult:
    """일회성 격리 실행 래퍼."""
    return Sandbox(timeout_sec=timeout_sec).run_code(code, stdin_payload=stdin_payload)
