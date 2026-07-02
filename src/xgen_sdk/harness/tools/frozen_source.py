"""FrozenToolSource — 컴파일 산출물(python wheel)의 standalone 도구 실행기.

node-engine 의 ``src/tools/dispatch.ts`` 와 1:1 패리티. 같은 ``spec.tool_definitions``
(call_kind / call_spec) 를 받아 동일하게 dispatch 한다. cluster 의존 0 — 모든 외부
자원(외부 API / Qdrant / MCP 서버)을 frozen 메타 + 외부 env 만으로 직접 호출.

call_kind 매트릭스 (dispatch.ts 와 동일):
  - http        : httpx 직접 외부 API. secrets_keys / secret_header_map / secret_body_map /
                  query_template / body_template / path_params 지원.
  - rag         : QDRANT_URL env + call_spec.embedder 메타 둘 다 있으면 Qdrant + 임베더
                  직접 호출. 하나라도 없으면 metadata.rag_endpoint / HARNESS_RAG_ENDPOINT shim 폴백.
  - mcp_session : call_spec.spawn(server_command) 있으면 stdio 직접 spawn(mcp SDK).
                  없으면 station_url / MCP_STATION_BASE_URL 프록시 폴백.
  - subpipeline : call_spec.config(중첩 harness_config) + tool_definitions(B 의 leaf
                  도구) 로 nested Pipeline 을 in-process 실행 (env-only, cluster 0).
                  워크플로우를 도구로 마는 경우 — 재귀 깊이 가드 적용.
  - canvas      : call_spec.graph(agentflow 캔버스 nodes/edges) 를 다중포트 DAG 로
                  in-process 실행 (env-only). 실행노드는 "call"(FrozenToolDefinition
                  dispatch — agent=subpipeline/rag/http 재사용) + transform/foreach/
                  router/passthrough. 워크플로우를 도구로 마는 경우의 그래프 실행기.
  - noop        : "(noop)".

도구 호출 시점에 필요한 env 가 미설정이면 ``content`` 에 어떤 env 를 설정해야 하는지
명시 안내(``_env_missing_msg``) 를 담아 반환 — 외부 실행자가 무엇을 wire 해야 하는지 즉시 파악.

이 소스는 ``register_tool_source()`` 로 등록되며 ``s07_act`` 가 ``call_tool`` 로 dispatch.
``metadata`` 는 컴파일된 spec.metadata (rag_endpoint/station_url 등 — BYO 시 비어있음).
"""

from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
import os
import socket
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_MAX_CONTENT = 50_000

# SSRF 가드 — 항상 차단하는 호스트명(스킴 무관).
_SSRF_BLOCK_NAMES = {"localhost", "metadata", "metadata.google.internal"}


def _host_is_blocked(host: str, *, block_private: bool = False) -> bool:
    """SSRF 방어 — 호스트가 loopback/link-local(클라우드 메타데이터)/예약 IP 면 True.

    기본은 loopback + link-local(169.254/16, 메타데이터 자격증명 탈취 벡터) + localhost/
    metadata 이름만 차단(외부 SaaS 호출은 안 막음). block_private=True 면 RFC1918 사설망도.
    이름 해석 실패는 차단 안 함(false-positive 회피 — httpx 가 알아서 실패).
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h in _SSRF_BLOCK_NAMES or h.endswith(".localhost"):
        return True
    ips: list[str] = []
    try:
        ipaddress.ip_address(h)
        ips = [h]
    except ValueError:
        try:
            ips = [ai[4][0] for ai in socket.getaddrinfo(h, None)]
        except Exception:
            return False
    for ip_s in ips:
        try:
            ip = ipaddress.ip_address(ip_s)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return True
        if block_private and ip.is_private:
            return True
    return False

# nested subpipeline 재귀 깊이 가드 — 워크플로우 A 가 도구로 B 를, B 가 다시 A 를
# 부르는 순환/폭주를 막는다. ContextVar 라 async 호출 트리마다 정확히 누적·복원.
_SUBPIPELINE_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_xgen_subpipeline_depth", default=0,
)
_MAX_SUBPIPELINE_DEPTH = 4


def _env_missing_msg(tool_name: str, missing: list[str], how: str = "") -> str:
    """도구 호출에 필요한 env 미설정 안내 — 외부 실행자가 무엇을 설정해야 하는지 명시."""
    names = ", ".join(missing)
    msg = f"Environment variables required to run tool '{tool_name}' are not set: {names}."
    if how:
        msg += f" {how}"
    msg += " Set these values in the external runtime (or the env section of your MCP client config) and retry."
    return msg


def _render_template(tmpl: dict, args: dict) -> dict:
    """``"{{name}}"`` 패턴을 args 값으로 치환. 미매치 항목은 생략(optional). dispatch.ts renderTemplate 동일."""
    out: dict = {}
    for k, v in tmpl.items():
        if isinstance(v, str):
            if len(v) >= 5 and v.startswith("{{") and v.endswith("}}"):
                arg_name = v[2:-2]
                if arg_name in args and args[arg_name] is not None:
                    out[k] = args[arg_name]
                # 미매치는 생략
                continue
            out[k] = v
        elif isinstance(v, list):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _render_template(v, args)
        else:
            out[k] = v
    return out


def _build_query_string(params: dict) -> str:
    pairs: list[tuple[str, str]] = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            for item in v:
                pairs.append((k, str(item)))
        else:
            pairs.append((k, str(v)))
    return urlencode(pairs)


class FrozenToolSource:
    """spec.tool_definitions 를 받아 call_kind 별로 직접 dispatch 하는 ToolSource.

    ``ToolSource`` Protocol (list_tools / call_tool / has_tool) 구현.
    """

    def __init__(
        self,
        tool_definitions: list[dict],
        *,
        metadata: Optional[dict] = None,
        source_id: str = "frozen",
    ) -> None:
        self._defs: dict[str, dict] = {}
        for td in tool_definitions or []:
            if isinstance(td, dict) and td.get("name"):
                self._defs[str(td["name"])] = td
        self._metadata = dict(metadata or {})
        self.source_id = source_id
        self.display_name = "Compiled tools"

    # ── ToolSource Protocol ──────────────────────────────────────────
    async def list_tools(self, filters: Optional[dict] = None) -> list[dict]:
        out: list[dict] = []
        for name, td in self._defs.items():
            out.append({
                "name": name,
                "description": td.get("description", ""),
                "input_schema": td.get("input_schema") or {"type": "object"},
            })
        return out

    def has_tool(self, name: str) -> bool:
        return name in self._defs

    async def call_tool(self, name: str, args: dict) -> dict:
        td = self._defs.get(name)
        if td is None:
            return {"content": f"Tool '{name}' is not defined", "is_error": True}
        kind = str(td.get("call_kind") or "noop")
        args = args or {}
        try:
            if kind == "http":
                return await self._dispatch_http(td, args)
            if kind == "rag":
                return await self._dispatch_rag(td, args)
            if kind == "mcp_session":
                return await self._dispatch_mcp(td, args)
            if kind == "subpipeline":
                return await self._dispatch_subpipeline(td, args)
            if kind == "canvas":
                return await self._dispatch_canvas(td, args)
            return {"content": "(noop)", "is_error": False}
        except Exception as e:  # 방어 — 어떤 dispatch 도 파이프라인을 죽이지 않음
            logger.warning("[frozen] tool=%s dispatch 예외: %s", name, e)
            return {"content": f"{name} execution error: {e}", "is_error": True}

    # ── http ─────────────────────────────────────────────────────────
    async def _dispatch_http(self, td: dict, args: dict) -> dict:
        spec = td.get("call_spec") or {}
        name = td.get("name", "")
        url = str(spec.get("url") or "")
        if not url:
            return {"content": "http call_spec.url is missing", "is_error": True}
        method = str(spec.get("method") or "POST").upper()
        headers: dict[str, str] = dict(spec.get("headers") or {})

        # 시크릿 env → 헤더/바디 inject. 미설정 env 를 모아 두었다가 호출 실패 시 안내.
        # 주입한 시크릿 값은 따로 모아 두었다가 에러 응답에서 마스킹한다(누출 방지).
        missing: list[str] = []
        secret_values: list[str] = []
        for key in (spec.get("secrets_keys") or []):
            val = os.environ.get(str(key))
            if val:
                headers[str(key)] = val
                secret_values.append(val)
            else:
                missing.append(str(key))
        for header_name, env_key in (spec.get("secret_header_map") or {}).items():
            val = os.environ.get(str(env_key))
            if val:
                headers[str(header_name)] = val
                secret_values.append(val)
            else:
                missing.append(str(env_key))
        secret_args: dict[str, Any] = {}
        for placeholder, env_key in (spec.get("secret_body_map") or {}).items():
            val = os.environ.get(str(env_key))
            if val:
                secret_args[str(placeholder)] = val
                secret_values.append(val)
            else:
                missing.append(str(env_key))

        def _redact(s: str) -> str:
            for sv in secret_values:
                if sv:
                    s = s.replace(sv, "***")
            return s

        render_args = {**args, **secret_args}
        query_params = _render_template(dict(spec.get("query_template") or {}), render_args)
        body_merged = {**_render_template(dict(spec.get("body_template") or {}), render_args), **args}

        # URL path placeholder 치환 ({owner}/{repo} 등). 소비된 키는 query/body 에서 제외.
        consumed: set[str] = set()

        def _sub(m):
            n = m.group(1)
            v = render_args.get(n)
            if v is None:
                return m.group(0)
            consumed.add(n)
            return quote(str(v), safe="")

        import re as _re
        resolved_url = _re.sub(r"\{(\w+)\}", _sub, url)
        for p in (spec.get("path_params") or []):
            consumed.add(str(p))
        for k in consumed:
            query_params.pop(k, None)
            body_merged.pop(k, None)

        final_url = resolved_url
        has_query = len(query_params) > 0
        if has_query or method in ("GET", "DELETE"):
            if has_query:
                qp_source = query_params
            elif method in ("GET", "DELETE"):
                qp_source = {k: v for k, v in args.items() if k not in consumed}
            else:
                qp_source = {}
            qs = _build_query_string(qp_source)
            if qs:
                final_url = resolved_url + ("&" if "?" in resolved_url else "?") + qs

        # SSRF 가드 — 최종 URL host 가 내부/링크로컬(메타데이터)면 차단. LLM args 가
        # URL placeholder 를 채우므로 내부망/169.254.169.254 로 유도될 수 있다.
        # spec.allow_internal=True 면 우회(운영자 내부 API 의도), block_private_hosts=True 면 RFC1918 도.
        _parsed = urlparse(final_url)
        if _parsed.scheme not in ("http", "https"):
            return {"content": f"Blocked scheme: {_parsed.scheme or '(none)'}", "is_error": True}
        if not bool(spec.get("allow_internal")) and _host_is_blocked(
            _parsed.hostname or "", block_private=bool(spec.get("block_private_hosts"))
        ):
            return {
                "content": f"SSRF blocked: internal/link-local host '{_parsed.hostname}' "
                           f"(set call_spec.allow_internal=true to allow)",
                "is_error": True,
            }

        req_headers = {"content-type": "application/json", **headers}
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                if method in ("GET", "DELETE"):
                    resp = await client.request(method, final_url, headers=req_headers)
                else:
                    resp = await client.request(
                        method, final_url, headers=req_headers, content=json.dumps(body_merged),
                    )
            text = resp.text
            if resp.status_code >= 400:
                hint = ""
                if missing:
                    hint = " — " + _env_missing_msg(name, missing)
                # 업스트림 4xx 본문이 인증헤더/서명파라미터를 echo 할 수 있어 시크릿 마스킹.
                return {"content": _redact(f"{resp.status_code} {text[:500]}{hint}"), "is_error": True}
            return {"content": text[:_MAX_CONTENT], "is_error": False}
        except Exception as e:
            # 예외 문자열에 URL(쿼리스트링 시크릿)이 섞일 수 있어 마스킹.
            return {"content": _redact(str(e)), "is_error": True}

    # ── rag ──────────────────────────────────────────────────────────
    async def _dispatch_rag(self, td: dict, args: dict) -> dict:
        spec = td.get("call_spec") or {}
        name = td.get("name", "")
        collection = str(spec.get("collection_name") or "")
        query = str(args.get("query") or "")
        top_k = int(spec.get("top_k") or 4)
        score_threshold = float(spec.get("score_threshold") or 0.0)

        qdrant_url_env = str(spec.get("qdrant_url_env") or "QDRANT_URL")
        qdrant_url = (os.environ.get(qdrant_url_env) or "").strip()
        embedder_meta = spec.get("embedder")

        # 1) 외부 자족 — QDRANT_URL + embedder 메타 둘 다 있으면 Qdrant 직접
        if qdrant_url and isinstance(embedder_meta, dict):
            try:
                from ..adapters.embedders import build_embedder
                embed = build_embedder(embedder_meta)
                vector = await embed(query)
                qdrant_api_key_env = str(spec.get("qdrant_api_key_env") or "QDRANT_API_KEY")
                qdrant_api_key = (os.environ.get(qdrant_api_key_env) or "").strip()
                result = await self._qdrant_search(
                    qdrant_url, qdrant_api_key, collection, vector, top_k, score_threshold,
                )
                return {"content": json.dumps(result, ensure_ascii=False)[:_MAX_CONTENT], "is_error": False}
            except Exception as e:
                direct_err = str(e)
                fb = await self._rag_via_shim(spec, query, name)
                if not fb["is_error"]:
                    return fb
                return {
                    "content": f"rag direct failed ({direct_err}); shim fallback failed ({fb['content']})",
                    "is_error": True,
                }

        # 2) shim 폴백
        return await self._rag_via_shim(spec, query, name)

    async def _rag_via_shim(self, spec: dict, query: str, tool_name: str) -> dict:
        endpoint = (
            str(self._metadata.get("rag_endpoint") or "")
            or os.environ.get("HARNESS_RAG_ENDPOINT")
            or ""
        )
        if not endpoint:
            qdrant_url_env = str(spec.get("qdrant_url_env") or "QDRANT_URL")
            return {
                "content": _env_missing_msg(
                    tool_name, [qdrant_url_env, "HARNESS_RAG_ENDPOINT"],
                    how=f"RAG search needs either (a) {qdrant_url_env} for direct external "
                        "Qdrant calls (+ embedder key) or (b) a RAG search endpoint "
                        "HARNESS_RAG_ENDPOINT.",
                ),
                "is_error": True,
            }
        body = {
            "collection_name": spec.get("collection_name"),
            "query": query,
            "top_k": spec.get("top_k", 4),
            "score_threshold": spec.get("score_threshold", 0.0),
        }
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    endpoint, headers={"content-type": "application/json"}, content=json.dumps(body),
                )
            text = resp.text
            if resp.status_code >= 400:
                return {"content": f"rag {resp.status_code}: {text[:500]}", "is_error": True}
            return {"content": text[:_MAX_CONTENT], "is_error": False}
        except Exception as e:
            return {"content": str(e), "is_error": True}

    async def _qdrant_search(
        self, qdrant_url: str, qdrant_api_key: str, collection: str,
        vector: list[float], top_k: int, score_threshold: float,
    ) -> list[dict]:
        url = f"{qdrant_url.rstrip('/')}/collections/{quote(collection, safe='')}/points/search"
        headers = {"content-type": "application/json"}
        if qdrant_api_key:
            headers["api-key"] = qdrant_api_key
        body: dict[str, Any] = {"vector": vector, "limit": top_k, "with_payload": True}
        if score_threshold > 0:
            body["score_threshold"] = score_threshold
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, content=json.dumps(body))
        if resp.status_code >= 400:
            raise RuntimeError(f"qdrant search {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return [
            {"id": r.get("id"), "score": r.get("score"), "payload": r.get("payload") or {}}
            for r in (data.get("result") or [])
        ]

    # ── mcp_session ──────────────────────────────────────────────────
    async def _dispatch_mcp(self, td: dict, args: dict) -> dict:
        spec = td.get("call_spec") or {}
        name = td.get("name", "")
        sid = str(spec.get("session_id") or "")
        if not sid:
            return {"content": "session_id is missing", "is_error": True}

        # 1) 외부 자족 — spawn.server_command 있으면 stdio 직접 spawn
        spawn = spec.get("spawn")
        if isinstance(spawn, dict) and spawn.get("server_command"):
            # env_keys 미설정 선검사 → 명시 안내
            missing = [
                str(k) for k in (spawn.get("env_keys") or [])
                if not os.environ.get(str(k))
            ]
            if missing:
                return {
                    "content": _env_missing_msg(
                        name, missing,
                        how=f"Required to launch MCP server '{spawn.get('server_command')}'.",
                    ),
                    "is_error": True,
                }
            try:
                return await self._mcp_via_spawn(name, args, spawn)
            except Exception as e:
                direct_err = str(e)
                station_url = (
                    str(self._metadata.get("station_url") or "")
                    or os.environ.get("MCP_STATION_BASE_URL") or ""
                )
                if station_url:
                    fb = await self._mcp_via_station(name, sid, args, station_url)
                    if not fb["is_error"]:
                        return fb
                    return {"content": f"mcp spawn failed ({direct_err}); station fallback failed ({fb['content']})", "is_error": True}
                return {"content": f"mcp spawn failed: {direct_err}", "is_error": True}

        # 2) station proxy 폴백
        station_url = (
            str(self._metadata.get("station_url") or "")
            or os.environ.get("MCP_STATION_BASE_URL") or ""
        )
        if not station_url:
            return {
                "content": _env_missing_msg(
                    name, ["MCP_STATION_BASE_URL"],
                    how="MCP tools need either (a) stdio spawn metadata frozen at compile time or "
                        "(b) the MCP Station proxy address MCP_STATION_BASE_URL.",
                ),
                "is_error": True,
            }
        return await self._mcp_via_station(name, sid, args, station_url)

    async def _mcp_via_station(self, tool_name: str, sid: str, args: dict, station_url: str) -> dict:
        url = f"{station_url.rstrip('/')}/api/mcp/mcp-request"
        payload = {"session_id": sid, "method": "tools/call", "params": {"name": tool_name, "arguments": args}}
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(url, headers={"content-type": "application/json"}, content=json.dumps(payload))
            text = resp.text
            if resp.status_code >= 400:
                return {"content": f"station {resp.status_code}: {text[:500]}", "is_error": True}
            try:
                data = json.loads(text)
                result = data.get("data") or data.get("result") or {}
                return {"content": _flatten_mcp_content(result), "is_error": False}
            except Exception:
                return {"content": text[:_MAX_CONTENT], "is_error": False}
        except Exception as e:
            return {"content": str(e), "is_error": True}

    async def _mcp_via_spawn(self, tool_name: str, args: dict, spawn: dict) -> dict:
        """mcp python SDK (stdio) 로 직접 서버 구동 + 도구 호출.

        호출 단위 spawn(open→call→close) — 단순/정확. 다회 호출 성능 최적화는 후속.
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = str(spawn.get("server_command") or "")
        server_args = [str(a) for a in (spawn.get("server_args") or [])]
        # 보안: 전 os.environ 을 child 에 넘기면 호스트의 **모든 시크릿**(타 provider 키 등)이
        # spawn 된 MCP 서버에 노출된다. 선언된 env_keys 만 전달 + 프로세스 구동에 필요한
        # 최소 시스템 변수만 화이트리스트(주석이 말하던 'env_keys 만'을 실제로 구현).
        _BASE_ENV_KEYS = (
            "PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC",
            "TEMP", "TMP", "HOME", "LANG", "LC_ALL", "TZ", "NODE_PATH",
        )
        child_env: dict[str, str] = {}
        for _k in _BASE_ENV_KEYS:
            _v = os.environ.get(_k)
            if isinstance(_v, str):
                child_env[_k] = _v
        for _k in (spawn.get("env_keys") or []):
            _v = os.environ.get(str(_k))
            if isinstance(_v, str):
                child_env[str(_k)] = _v
        params = StdioServerParameters(
            command=command, args=server_args, env=child_env,
            cwd=str(spawn["working_dir"]) if spawn.get("working_dir") else None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=args)
        content = getattr(result, "content", None)
        is_error = bool(getattr(result, "isError", False))
        return {"content": _flatten_mcp_content({"content": content}), "is_error": is_error}

    # ── subpipeline ──────────────────────────────────────────────────
    async def _dispatch_subpipeline(self, td: dict, args: dict) -> dict:
        """중첩 워크플로우를 도구로 실행 — cluster 0, http 0, stdio 0 (env-only).

        다른 하네스 워크플로우(B)를 도구로 마는 경우, B 의 harness_config 와 B 의
        leaf 도구들(http/rag/mcp = 자족 가능)이 call_spec 에 통째로 freeze 돼 있다.
        호출 시 그 config 로 nested Pipeline 을 in-process 빌드해 실행 — B 의 도구는
        state 범위로 격리 주입돼 부모 카탈로그를 오염시키지 않는다.
        """
        spec = td.get("call_spec") or {}
        name = td.get("name", "")
        sub_config = spec.get("config")
        if not isinstance(sub_config, dict) or not sub_config:
            return {"content": f"subpipeline '{name}': call_spec.config is missing", "is_error": True}
        sub_tool_defs = spec.get("tool_definitions") or []
        sub_meta = spec.get("metadata") or self._metadata or {}

        # harness-agents 규약은 {"input": "..."} — query/user_input 도 관용 허용.
        user_input = ""
        if isinstance(args, dict):
            user_input = args.get("input") or args.get("query") or args.get("user_input") or ""
        if not isinstance(user_input, str):
            user_input = str(user_input)

        depth = _SUBPIPELINE_DEPTH.get()
        if depth >= _MAX_SUBPIPELINE_DEPTH:
            return {
                "content": f"subpipeline max recursion depth ({_MAX_SUBPIPELINE_DEPTH}) exceeded — '{name}' aborted",
                "is_error": True,
            }
        token = _SUBPIPELINE_DEPTH.set(depth + 1)
        try:
            out = await _run_nested_pipeline(sub_config, sub_tool_defs, sub_meta, user_input)
            return {"content": out or "", "is_error": False}
        except Exception as e:
            logger.warning("[frozen] subpipeline '%s' 실행 실패: %s", name, e)
            return {"content": f"{name} subpipeline execution error: {e}", "is_error": True}
        finally:
            _SUBPIPELINE_DEPTH.reset(token)

    # ── canvas (agentflow 그래프 인터프리터) ──────────────────────────
    async def _dispatch_canvas(self, td: dict, args: dict) -> dict:
        """agentflow 캔버스(nodes/edges)를 다중포트 DAG 로 in-process 실행 (env-only)."""
        spec = td.get("call_spec") or {}
        name = td.get("name", "")
        graph = spec.get("graph")
        if not isinstance(graph, dict) or not graph.get("nodes"):
            return {"content": f"canvas '{name}': call_spec.graph is missing/empty", "is_error": True}
        meta = spec.get("metadata") or self._metadata or {}
        user_input = ""
        if isinstance(args, dict):
            user_input = args.get("input") or args.get("query") or args.get("user_input") or ""
        if not isinstance(user_input, str):
            user_input = str(user_input)

        depth = _SUBPIPELINE_DEPTH.get()
        if depth >= _MAX_SUBPIPELINE_DEPTH:
            return {"content": f"canvas max recursion depth ({_MAX_SUBPIPELINE_DEPTH}) exceeded — '{name}' aborted", "is_error": True}
        token = _SUBPIPELINE_DEPTH.set(depth + 1)
        try:
            out = await _run_canvas_graph(graph, meta, user_input)
            return {"content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str), "is_error": False}
        except Exception as e:
            logger.warning("[frozen] canvas '%s' 실행 실패: %s", name, e)
            return {"content": f"{name} canvas execution error: {e}", "is_error": True}
        finally:
            _SUBPIPELINE_DEPTH.reset(token)


# ─── canvas 그래프 인터프리터 (다중포트 DAG) ────────────────────────────
# node = {id, kind, config, node_type?, out_ports?}. kind:
#   call        : config.tool = FrozenToolDefinition → FrozenToolSource 로 dispatch
#                 (agent=subpipeline / rag / http / mcp / 중첩 canvas 전부 재사용).
#   transform   : config.op = jmespath|template|passthrough → 순수 데이터 변환.
#   foreach     : config.items_port(리스트 입력) + config.body(FrozenToolDefinition) 반복.
#   router      : config.criteria — 입력을 ok/fail 포트로 분기(v1: best-effort).
#   passthrough : input/output/start/end — 입력 그대로 통과.
#   unsupported : 미지원 노드 — 입력 통과 + 경고(그래프 흐름 유지, 정직).

def _stringify(v: Any) -> str:
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        return str(v)


def _canvas_out_ports(node: dict) -> list:
    op = node.get("out_ports")
    if isinstance(op, list) and op:
        return [str(x) for x in op]
    return ["result", "output", "*"]


def _collect_canvas_input(nid: str, incoming: dict, outputs: dict, user_input: str) -> Any:
    """노드 입력 — 들어오는 edge 들의 source 출력값 수집. 없으면 entry=user_input."""
    inc = incoming.get(nid) or []
    if not inc:
        return user_input
    vals: dict = {}
    for (dst_port, src_nid, src_port) in inc:
        src_out = outputs.get(src_nid) or {}
        v = src_out.get(src_port, src_out.get("*"))
        if v is not None:
            vals[dst_port] = v
    if not vals:
        return user_input
    if len(vals) == 1:
        return next(iter(vals.values()))
    # 다중 입력 — 포트별 dict (transform 은 dict 로, call 은 합쳐서 사용)
    return vals


async def _run_canvas_node(node: dict, inval: Any, metadata: dict) -> Any:
    kind = str(node.get("kind") or "passthrough")
    cfg = node.get("config") or {}
    if kind in ("passthrough", "input", "output"):
        return inval
    if kind == "call":
        tool = cfg.get("tool")
        if not isinstance(tool, dict) or not tool.get("name"):
            return _stringify(inval)
        # 입력을 도구 인자로 — dict 면 그대로 + input 키, 아니면 input 문자열.
        if isinstance(inval, dict):
            args = dict(inval)
            args.setdefault("input", _stringify(inval))
        else:
            args = {"input": _stringify(inval), "query": _stringify(inval)}
        src = FrozenToolSource([tool], metadata=metadata)
        res = await src.call_tool(str(tool["name"]), args)
        return (res or {}).get("content", "") if isinstance(res, dict) else _stringify(res)
    if kind == "transform":
        return _canvas_transform(cfg, inval)
    if kind == "foreach":
        return await _canvas_foreach(cfg, inval, metadata)
    if kind == "router":
        # v1: 조건 충실평가 대신 입력 통과(ok 경로). criteria 평가는 후속.
        return inval
    # unsupported — 흐름 유지 위해 통과(정직: 변환 안 함)
    logger.info("[frozen/canvas] 미지원 노드 kind=%s type=%s — passthrough", kind, node.get("node_type"))
    return inval


def _canvas_transform(cfg: dict, inval: Any) -> Any:
    op = str(cfg.get("op") or "passthrough")
    if op == "jmespath":
        expr = cfg.get("expr") or ""
        try:
            import jmespath  # optional
            data = inval if not isinstance(inval, str) else (json.loads(inval) if inval.strip().startswith(("{", "[")) else inval)
            return jmespath.search(expr, data)
        except Exception as e:
            logger.info("[frozen/canvas] jmespath 변환 skip (%s) — passthrough", e)
            return inval
    if op == "template":
        tmpl = str(cfg.get("template") or "")
        if not tmpl:
            return inval
        ctx = inval if isinstance(inval, dict) else {"input": _stringify(inval)}
        out = tmpl
        for k, v in ctx.items():
            out = out.replace("{{" + str(k) + "}}", _stringify(v))
        return out
    return inval


async def _canvas_foreach(cfg: dict, inval: Any, metadata: dict) -> Any:
    """items 리스트를 순회하며 body(FrozenToolDefinition)를 각 항목에 실행 → 결과 리스트."""
    body = cfg.get("body")
    items = inval
    if isinstance(inval, dict):
        items = inval.get(cfg.get("items_port") or "items") or inval.get("items") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            items = [items]
    if not isinstance(items, list):
        items = [items]
    if not isinstance(body, dict) or not body.get("name"):
        return items  # body 없으면 항목 그대로
    src = FrozenToolSource([body], metadata=metadata)
    results = []
    for it in items:
        args = {"input": _stringify(it), "query": _stringify(it)}
        if isinstance(it, dict):
            args = {**it, "input": _stringify(it)}
        res = await src.call_tool(str(body["name"]), args)
        results.append((res or {}).get("content", "") if isinstance(res, dict) else _stringify(res))
    return results


async def _run_canvas_graph(graph: dict, metadata: dict, user_input: str) -> str:
    """다중포트 DAG 위상 실행 → sink(출력없는 노드) 또는 마지막 실행값 반환."""
    nodes: dict = {}
    for n in (graph.get("nodes") or []):
        nid = n.get("id")
        if nid:
            nodes[str(nid)] = n
    if not nodes:
        return ""
    edges = graph.get("edges") or []
    incoming: dict = {}     # tn -> [(dst_port, src_nid, src_port)]
    upstream: dict = {}     # tn -> set(src_nid)
    has_out: set = set()    # src_nid 가 outgoing edge 보유
    for e in edges:
        s = e.get("source") or {}
        t = e.get("target") or {}
        sn, sp = str(s.get("nodeId") or ""), str(s.get("portId") or "")
        tn, tp = str(t.get("nodeId") or ""), str(t.get("portId") or "")
        if not sn or not tn or sn not in nodes or tn not in nodes:
            continue
        incoming.setdefault(tn, []).append((tp, sn, sp))
        upstream.setdefault(tn, set()).add(sn)
        has_out.add(sn)

    outputs: dict = {}
    done: set = set()
    last_value: Any = user_input
    guard = 0
    limit = len(nodes) * 4 + 10
    while len(done) < len(nodes) and guard < limit:
        guard += 1
        progressed = False
        for nid, node in nodes.items():
            if nid in done:
                continue
            ups = upstream.get(nid, set())
            if not all(u in done for u in ups):
                continue
            inval = _collect_canvas_input(nid, incoming, outputs, user_input)
            try:
                val = await _run_canvas_node(node, inval, metadata)
            except Exception as e:
                logger.warning("[frozen/canvas] 노드 %s 실행 오류: %s", nid, e)
                val = inval
            port_map = {"*": val}
            for op in _canvas_out_ports(node):
                port_map[op] = val
            outputs[nid] = port_map
            last_value = val
            done.add(nid)
            progressed = True
        if not progressed:
            break  # 사이클/교착 — 남은 노드 skip

    # 최종 출력 — sink(outgoing 없는 노드) 중 마지막 실행값, 없으면 last_value
    sink_val = None
    for nid in nodes:
        if nid in outputs and nid not in has_out:
            sink_val = outputs[nid].get("*")
    out = sink_val if sink_val is not None else last_value
    return out if isinstance(out, str) else _stringify(out)


async def _run_nested_pipeline(
    config_dict: dict, tool_definitions: list, metadata: dict, user_input: str,
) -> str:
    """frozen sub-config 로 nested Pipeline 빌드·실행 → final_output 반환.

    생성 wheel 의 flow.build_pipeline 과 동일한 env→provider / doc_service wiring
    (패리티). 차이는 sub 도구를 전역 register 대신 state.extra_tool_sources 로
    격리 주입한다는 점 — 부모/자식 파이프라인 도구 카탈로그가 섞이지 않는다.
    """
    from .. import HarnessConfig, Pipeline, PipelineState
    from ..config import DictConfigSource, EnvConfigSource
    from ..adapters import create_provider

    config = HarnessConfig.resolve(sources=[
        EnvConfigSource(prefix="XGEN_HARNESS_"),
        DictConfigSource(dict(config_dict)),
    ])

    # provider — env 우선, 없으면 sub-config 의 provider/model. API key 는 env 만.
    provider_name = os.environ.get("XGEN_HARNESS_PROVIDER") or config.provider or "openai"
    env_map = {
        "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
        "google": "GEMINI_API_KEY", "vllm": "VLLM_API_KEY",
    }
    env_key = env_map.get(provider_name, f"{provider_name.upper()}_API_KEY")
    api_key = os.environ.get(env_key, "")
    if not api_key and provider_name in ("vllm", "google", "bedrock"):
        api_key = "EMPTY"
    base_url = (
        os.environ.get("XGEN_HARNESS_BASE_URL")
        or os.environ.get(f"{provider_name.upper()}_BASE_URL")
    )
    model_override = os.environ.get("XGEN_HARNESS_MODEL") or config.model or None
    provider_kwargs: dict = {"api_key": api_key, "model": model_override}
    if base_url:
        provider_kwargs["base_url"] = base_url
    provider = create_provider(provider_name, **provider_kwargs)

    # doc_service — QDRANT_URL 있으면 Qdrant 직격 (RAG leaf 도구용).
    doc_service = None
    qdrant_url = os.environ.get("QDRANT_URL")
    if qdrant_url:
        from ..adapters import QdrantDocService
        embedder = None
        meta = config_dict.get("_rag_embedder")
        if isinstance(meta, dict) and meta.get("provider"):
            try:
                from ..adapters import build_embedder, discover_external_embedders
                discover_external_embedders()
                embedder = build_embedder(meta)
            except Exception as e:  # provider 미등록/키 누락 — embedder 없이 진행
                logger.warning("[frozen] nested doc_service embedder build 실패: %s", e)
        doc_service = QdrantDocService(
            url=qdrant_url, api_key=os.environ.get("QDRANT_API_KEY"), embedder=embedder,
        )

    # sub 도구는 같은 source_id("frozen") 로 — 단 state 범위 주입이라 전역 충돌 없음.
    nested_source = FrozenToolSource(tool_definitions or [], metadata=metadata or {})
    pipe = Pipeline.from_config(config, provider=provider, doc_service=doc_service)
    state = PipelineState(user_input=user_input)
    state.extra_tool_sources = [nested_source]
    result = await pipe.run(state)
    return getattr(result, "final_output", "") or ""


def _flatten_mcp_content(result: dict) -> str:
    """MCP tools/call 결과의 content 블록 배열 → 평문. dispatch.ts 동일 로직."""
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(str(text))
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "\n".join(parts)
    try:
        return json.dumps(result, ensure_ascii=False, default=str)[:_MAX_CONTENT]
    except Exception:
        return str(result)[:_MAX_CONTENT]
