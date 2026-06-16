"""Sandbox Verifiers — 외부 프로세스를 격리 환경에서 **검증**하는 확장 지점 (v0.20.0).

`core/sandbox.py` 의 `Sandbox` 가 "한 번 실행하고 끝" 이라면, verifier 는 "long-running stdio
서버를 잠시 띄워 왕복 검증 후 정리" 하는 역할. 컴파일된 MCP 서버를 xgen-mcp-station 같은
외부 호스트에 등록하기 **전에** 건전성·스키마·리소스 한계를 검증하는 문지기 (Gate).

설계 원칙
---------
1. **Protocol + Registry + entry_points** — 하드코딩된 if/elif 없음. 새 프로토콜
   (mcp-http, docker-wrapped, wasm 등) 은 `register_sandbox_verifier()` 한 줄 또는
   `xgen_harness.sandbox_verifiers` entry_points 그룹으로 추가.
2. **엔진 generic primitive** — 특정 서비스(xgen-mcp-station / Claude API)를 모름.
   호출자가 `command: list[str]` 을 준비해서 넘기고, verifier 는 그 명령을 Popen.
3. **격리 재사용** — POSIX rlimit (`SandboxLimits`) / timeout / stderr tail cap 은
   `core/sandbox.py` 와 같은 정책으로 통일.

기본 verifier
-------------
- **mcp-stdio** (`MCPStdioVerifier`): JSON-RPC over stdio. initialize → initialized
  notification → tools/list 왕복, 스키마 유효성 확인, 정규화된 tools 배열의 SHA-256
  해시 반환(재현성 지표).

사용례
------
    >>> from xgen_harness import MCPStdioVerifier, SandboxLimits
    >>> v = MCPStdioVerifier()
    >>> r = v.verify(
    ...     command=["python", "-u", "-m", "xgen_gallery_foo.cli", "serve-mcp"],
    ...     timeout_sec=10.0,
    ...     limits=SandboxLimits(cpu_seconds=15, address_space_mb=1024),
    ... )
    >>> assert r.ok, r.error
    >>> print(r.tool_count, r.payload_hash[:12])

Phase B 위치 (v0.20.0).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from .sandbox import DEFAULT_LIMITS, SandboxLimits

logger = logging.getLogger("harness.sandbox.verifier")


# ────────────────────────────────────────────────────────────────
#  Result
# ────────────────────────────────────────────────────────────────


@dataclass
class VerifyResult:
    """검증 결과.

    ok=False 일 때 error 에 실패 원인 요약, stderr_tail 에 프로세스 stderr 마지막 일부.
    """

    ok: bool
    verifier: str
    tools: list[dict] = field(default_factory=list)
    tool_count: int = 0
    handshake_ms: int = 0
    tools_ms: int = 0
    payload_hash: str = ""
    stderr_tail: str = ""
    error: Optional[str] = None
    exit_code: Optional[int] = None
    timed_out: bool = False
    applied_limits: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verifier": self.verifier,
            "tool_count": self.tool_count,
            "tools": [t.get("name") for t in self.tools if isinstance(t, dict)],
            "handshake_ms": self.handshake_ms,
            "tools_ms": self.tools_ms,
            "payload_hash": self.payload_hash,
            "error": self.error,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "applied_limits": self.applied_limits,
        }


# ────────────────────────────────────────────────────────────────
#  Protocol
# ────────────────────────────────────────────────────────────────


@runtime_checkable
class SandboxVerifier(Protocol):
    name: str

    def verify(
        self,
        *,
        command: list[str],
        env: Optional[dict[str, str]] = None,
        limits: Optional[SandboxLimits] = None,
        timeout_sec: float = 10.0,
    ) -> VerifyResult:
        ...


# ────────────────────────────────────────────────────────────────
#  Registry
# ────────────────────────────────────────────────────────────────


_REGISTRY: dict[str, SandboxVerifier] = {}
_BOOTSTRAPPED = False


def register_sandbox_verifier(name: str, verifier: SandboxVerifier) -> None:
    """SandboxVerifier 를 레지스트리에 등록.

    name 중복 시 override (플러그인이 내장 대체 가능).
    """
    if not isinstance(verifier, SandboxVerifier):
        raise TypeError(f"verifier '{name}' does not satisfy SandboxVerifier Protocol")
    if name in _REGISTRY:
        logger.info("[sandbox-verifier] override: %s", name)
    _REGISTRY[name] = verifier


def get_sandbox_verifier(name: str) -> Optional[SandboxVerifier]:
    return _REGISTRY.get(name)


def list_sandbox_verifiers() -> list[str]:
    return sorted(_REGISTRY.keys())


def bootstrap_default_sandbox_verifiers() -> None:
    """기본 verifier 등록 + entry_points 자동 로드. 1회만 실행."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    register_sandbox_verifier("mcp-stdio", MCPStdioVerifier())

    try:
        if sys.version_info >= (3, 10):
            from importlib.metadata import entry_points
            eps = entry_points(group="xgen_harness.sandbox_verifiers")
        else:
            from importlib.metadata import entry_points
            eps = entry_points().get("xgen_harness.sandbox_verifiers", [])
        for ep in eps:
            try:
                factory = ep.load()
                v = factory() if callable(factory) else factory
                if isinstance(v, SandboxVerifier):
                    register_sandbox_verifier(getattr(v, "name", ep.name), v)
                    logger.info("[sandbox-verifier] loaded from entry_points: %s", ep.name)
                else:
                    logger.warning(
                        "[sandbox-verifier] entry_point %s did not return SandboxVerifier",
                        ep.name,
                    )
            except Exception as e:
                logger.warning("[sandbox-verifier] entry_point %s load failed: %s", ep.name, e)
    except Exception as e:
        logger.debug("[sandbox-verifier] entry_points scan skipped: %s", e)

    _BOOTSTRAPPED = True


# ────────────────────────────────────────────────────────────────
#  MCPStdioVerifier
# ────────────────────────────────────────────────────────────────


_RPC_VERSION = "2.0"
_MCP_PROTO = "2024-11-05"


class MCPStdioVerifier:
    """MCP JSON-RPC over stdio 기반 서버를 initialize + tools/list 왕복으로 검증.

    과정:
      1. `command` 를 Popen 으로 기동 (rlimit + isolated env).
      2. JSON-RPC initialize 요청 → response 대기 (timeout_sec).
      3. `notifications/initialized` 알림 송신.
      4. tools/list 요청 → response 대기.
      5. 자식 프로세스 stdin close → terminate → (지연 시) kill.
      6. tools 배열의 (name, description, inputSchema) 를 정규화해 SHA-256.

    성공 기준:
      - initialize response 의 serverInfo/capabilities 가 존재
      - tools/list response 의 tools 가 list (빈 list 도 허용 — 그래도 초기화는 된 것)
      - 양 단계 모두 timeout 내 응답
    """

    name: str = "mcp-stdio"

    def verify(
        self,
        *,
        command: list[str],
        env: Optional[dict[str, str]] = None,
        limits: Optional[SandboxLimits] = None,
        timeout_sec: float = 10.0,
    ) -> VerifyResult:
        if not command or not isinstance(command, list):
            return VerifyResult(ok=False, verifier=self.name, error="command is empty or not a list")

        lim = limits or DEFAULT_LIMITS
        preexec = _build_rlimit_preexec(lim)
        merged_env = _merged_env(env)

        start = time.time()
        proc: Optional[subprocess.Popen] = None
        stderr_chunks: list[bytes] = []
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                bufsize=0,
                preexec_fn=preexec,
            )
        except FileNotFoundError as e:
            return VerifyResult(
                ok=False, verifier=self.name,
                error=f"command not found: {e}",
                applied_limits=_limits_dict(lim),
            )
        except Exception as e:
            return VerifyResult(
                ok=False, verifier=self.name,
                error=f"popen failed: {e}",
                applied_limits=_limits_dict(lim),
            )

        try:
            # 1) initialize
            t0 = time.time()
            init_req = {
                "jsonrpc": _RPC_VERSION,
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTO,
                    "capabilities": {},
                    "clientInfo": {"name": "xgen-harness-verifier", "version": "1"},
                },
            }
            _send(proc, init_req)
            init_resp = _recv(proc, timeout_sec=timeout_sec, stderr_sink=stderr_chunks)
            handshake_ms = int((time.time() - t0) * 1000)

            if not _is_valid_init_response(init_resp):
                return _finalize_fail(
                    proc, stderr_chunks, verifier=self.name,
                    error=f"invalid initialize response: {_short(init_resp)}",
                    handshake_ms=handshake_ms, applied=_limits_dict(lim),
                )

            # 2) initialized notification (no response expected)
            _send(proc, {"jsonrpc": _RPC_VERSION, "method": "notifications/initialized"})

            # 3) tools/list
            t1 = time.time()
            _send(proc, {"jsonrpc": _RPC_VERSION, "id": 2, "method": "tools/list", "params": {}})
            tools_resp = _recv(proc, timeout_sec=timeout_sec, stderr_sink=stderr_chunks)
            tools_ms = int((time.time() - t1) * 1000)

            tools = _extract_tools(tools_resp)
            if tools is None:
                return _finalize_fail(
                    proc, stderr_chunks, verifier=self.name,
                    error=f"invalid tools/list response: {_short(tools_resp)}",
                    handshake_ms=handshake_ms, tools_ms=tools_ms,
                    applied=_limits_dict(lim),
                )

            payload_hash = _hash_tools(tools)

            # 4) graceful shutdown
            _stop(proc)
            _drain_stderr(proc, stderr_chunks)

            return VerifyResult(
                ok=True, verifier=self.name,
                tools=tools, tool_count=len(tools),
                handshake_ms=handshake_ms, tools_ms=tools_ms,
                payload_hash=payload_hash,
                stderr_tail=_join_stderr(stderr_chunks),
                exit_code=proc.returncode,
                applied_limits=_limits_dict(lim),
            )
        except _VerifierTimeout as e:
            _stop(proc)
            _drain_stderr(proc, stderr_chunks)
            return VerifyResult(
                ok=False, verifier=self.name,
                error=str(e),
                timed_out=True,
                stderr_tail=_join_stderr(stderr_chunks),
                exit_code=proc.returncode if proc else None,
                applied_limits=_limits_dict(lim),
            )
        except Exception as e:
            _stop(proc)
            _drain_stderr(proc, stderr_chunks)
            return VerifyResult(
                ok=False, verifier=self.name,
                error=f"unexpected error: {e}",
                stderr_tail=_join_stderr(stderr_chunks),
                exit_code=proc.returncode if proc else None,
                applied_limits=_limits_dict(lim),
            )
        finally:
            duration = time.time() - start
            logger.debug("[sandbox-verifier] mcp-stdio verify done in %.2fs", duration)


# ────────────────────────────────────────────────────────────────
#  Convenience
# ────────────────────────────────────────────────────────────────


def verify_mcp_stdio(
    command: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    limits: Optional[SandboxLimits] = None,
    timeout_sec: float = 10.0,
) -> VerifyResult:
    """`MCPStdioVerifier` 의 one-shot 래퍼."""
    return MCPStdioVerifier().verify(
        command=command, env=env, limits=limits, timeout_sec=timeout_sec,
    )


# ────────────────────────────────────────────────────────────────
#  Internal helpers
# ────────────────────────────────────────────────────────────────


class _VerifierTimeout(Exception):
    pass


def _build_rlimit_preexec(limits: SandboxLimits):
    """POSIX rlimit 적용 preexec_fn — Sandbox._build_preexec_fn 과 동일 정책."""
    if os.name != "posix":
        return None

    mb = 1024 * 1024

    def _apply():  # pragma: no cover
        try:
            import resource
            if limits.cpu_seconds > 0:
                resource.setrlimit(resource.RLIMIT_CPU,
                                   (limits.cpu_seconds, limits.cpu_seconds))
            if limits.address_space_mb > 0:
                b = limits.address_space_mb * mb
                resource.setrlimit(resource.RLIMIT_AS, (b, b))
            if limits.max_open_files > 0:
                n = limits.max_open_files
                resource.setrlimit(resource.RLIMIT_NOFILE, (n, n))
            if limits.max_file_size_mb > 0:
                b = limits.max_file_size_mb * mb
                resource.setrlimit(resource.RLIMIT_FSIZE, (b, b))
            if limits.no_core_dump:
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # 새 프로세스 그룹 — kill 시 자식들까지 정리 쉽게.
            try:
                os.setsid()
            except Exception:
                pass
        except Exception:
            pass

    return _apply


def _merged_env(extra: Optional[dict[str, str]]) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        for k, v in extra.items():
            env[str(k)] = str(v)
    # MCP 서버가 stdout 을 버퍼링하면 handshake 가 hang — unbuffered 요청.
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _send(proc: subprocess.Popen, payload: dict) -> None:
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    assert proc.stdin is not None
    proc.stdin.write(data)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen, *, timeout_sec: float,
          stderr_sink: list[bytes]) -> dict:
    """stdout 에서 개행으로 구분된 JSON 메시지 한 개를 읽어 반환.

    알림/응답 중 첫 dict 를 반환. timeout_sec 내에 안 오면 _VerifierTimeout.
    """
    assert proc.stdout is not None
    import select

    deadline = time.time() + timeout_sec
    buf = bytearray()
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise _VerifierTimeout(f"no response within {timeout_sec}s")
        rlist, _, _ = select.select([proc.stdout, proc.stderr], [], [], min(remaining, 0.5))
        if proc.stderr in rlist:
            chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
            if chunk:
                stderr_sink.append(chunk)
        if proc.stdout in rlist:
            ch = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not ch:
                # EOF — 프로세스가 죽은 상태
                raise _VerifierTimeout("child exited before response")
            buf.extend(ch)
            # 개행으로 메시지 단위 분리 — 첫 유효 JSON 반환
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                s = line.decode("utf-8", "replace").strip()
                if not s:
                    continue
                try:
                    msg = json.loads(s)
                except Exception:
                    continue
                if isinstance(msg, dict):
                    return msg
        if proc.poll() is not None and not rlist:
            raise _VerifierTimeout("child exited without response")


def _is_valid_init_response(resp: dict) -> bool:
    if not isinstance(resp, dict):
        return False
    if resp.get("error") is not None:
        return False
    result = resp.get("result")
    if not isinstance(result, dict):
        return False
    # serverInfo 또는 capabilities 둘 중 하나라도 있으면 유효로 본다 (MCP 구현별 차이 허용).
    return bool(result.get("serverInfo") or result.get("capabilities"))


def _extract_tools(resp: dict) -> Optional[list[dict]]:
    if not isinstance(resp, dict):
        return None
    if resp.get("error") is not None:
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    tools = result.get("tools")
    if not isinstance(tools, list):
        return None
    return [t for t in tools if isinstance(t, dict)]


def _hash_tools(tools: list[dict]) -> str:
    """tools 목록을 결정적으로 정렬 + 직렬화하여 SHA-256 해시.

    재현성 지표 — 같은 wheel 이면 같은 hash. 이식측이 Station 등록 시 메타에 첨부.
    """
    norm = []
    for t in tools:
        norm.append({
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "inputSchema": t.get("inputSchema") or {},
        })
    norm.sort(key=lambda x: x["name"])
    blob = json.dumps(norm, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _stop(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:
        pass
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass


def _drain_stderr(proc: Optional[subprocess.Popen], sink: list[bytes]) -> None:
    if proc is None or proc.stderr is None:
        return
    try:
        remaining = proc.stderr.read()
        if remaining:
            sink.append(remaining)
    except Exception:
        pass


def _join_stderr(chunks: list[bytes], *, cap: int = 4096) -> str:
    raw = b"".join(chunks)
    if len(raw) > cap:
        raw = raw[-cap:]
    return raw.decode("utf-8", "replace")


def _limits_dict(lim: SandboxLimits) -> dict:
    return {
        "cpu_seconds": lim.cpu_seconds,
        "address_space_mb": lim.address_space_mb,
        "max_open_files": lim.max_open_files,
        "max_file_size_mb": lim.max_file_size_mb,
        "posix": os.name == "posix",
    }


def _short(payload) -> str:
    try:
        s = json.dumps(payload, ensure_ascii=False)
    except Exception:
        s = str(payload)
    return s if len(s) <= 200 else s[:200] + "…"
