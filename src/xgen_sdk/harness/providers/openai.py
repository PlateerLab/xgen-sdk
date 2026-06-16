"""
OpenAI LLM Provider — httpx SSE 스트리밍

OpenAI Chat Completions API. tool_use를 Anthropic 포맷으로 통합 변환.

vLLM/Qwen 등 OpenAI-호환 endpoint 가 native tool_calls 필드 매핑을 못 하면
모델이 학습된 native XML 포맷 (`<tool_call>...</tool_call>`) 을 text content
에 박아 응답한다. 두 변형(Hermes JSON / XML parameter) 을 본 모듈에서 후처리
파싱해 ProviderEventType.TOOL_USE 로 변환하므로 vLLM serve 옵션에 의존하지
않는다.
"""

import json
import logging
import re
import uuid
from typing import Any, AsyncGenerator, Optional

import httpx

from .base import LLMProvider, ProviderEvent, ProviderEventType
from ..errors import ProviderError

logger = logging.getLogger("harness.provider.openai")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


# vLLM/Qwen native tool-call 텍스트 마커. Hermes (JSON body) 와 XML parameter
# (Qwen 일부 fine-tune) 둘 다 같은 outer tag.
_TOOL_CALL_OPEN = "<tool_call>"
_TOOL_CALL_CLOSE = "</tool_call>"
_TOOL_CALL_OPEN_LEN = len(_TOOL_CALL_OPEN)

# XML parameter 형식 — `<function=name>...<parameter=key>value</parameter>...</function>`
_FUNCTION_NAME_RE = re.compile(r"<function=([^>\s]+)\s*>", re.IGNORECASE)
_PARAMETER_RE = re.compile(
    r"<parameter=([^>\s]+)\s*>(.*?)</parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _parse_native_tool_call(body: str) -> Optional[dict]:
    """`<tool_call>` 안쪽 본문을 name + arguments dict 로 파싱.

    두 형식 모두 시도:
    1) Hermes JSON: `{"name": "x", "arguments": {...}}`
    2) XML param  : `<function=name><parameter=k>v</parameter>...</function>`

    실패 시 None — 호출부는 원본 텍스트 그대로 fallback emit.
    """
    s = (body or "").strip()
    if not s:
        return None

    # 1) JSON 시도
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and obj.get("name"):
                args = obj.get("arguments")
                if isinstance(args, str):
                    # arguments 가 문자열 JSON 일 수 있음 (Hermes variants)
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"raw": args}
                if not isinstance(args, dict):
                    args = {} if args is None else {"value": args}
                return {"name": str(obj["name"]), "arguments": args}
        except Exception as e:
            logger.debug("[openai] tool_call JSON parse 실패: %s", e)

    # 2) XML parameter 시도
    fn_match = _FUNCTION_NAME_RE.search(s)
    if fn_match:
        name = fn_match.group(1).strip()
        params: dict[str, Any] = {}
        for p_match in _PARAMETER_RE.finditer(s):
            key = p_match.group(1).strip()
            val = p_match.group(2).strip()
            params[key] = val
        return {"name": name, "arguments": params}

    return None


class OpenAIProvider(LLMProvider):
    """OpenAI API 프로바이더"""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini", base_url: Optional[str] = None):
        self._api_key = api_key
        self._model = model
        # base_url 이 base(예: "https://api.openai.com/v1") 만 와도 endpoint 자동 조립.
        # Anthropic provider 와 동일 패턴 (persistent_configs 에 base URL 저장 시 호환).
        from .base import normalize_base_url
        self._base_url = normalize_base_url(base_url or OPENAI_API_URL, api_path="chat/completions")

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model

    def supports_tool_use(self) -> bool:
        return True

    def supports_thinking(self) -> bool:
        return False

    # v0.11.22 — stream_options 를 수신하지 못하는 프록시/호환 엔드포인트에서
    # output_tokens=0 이 고정되는 문제를 tiktoken 으로 보정. tiktoken 미설치 환경은
    # base class 의 chars/3 휴리스틱으로 fallback.
    def count_tokens(self, text: str) -> tuple[int, str]:
        if not text:
            return 0, "empty"
        try:
            import tiktoken  # type: ignore
            try:
                enc = tiktoken.encoding_for_model(self._model)
            except Exception as e:
                # 알 수 없는 모델은 o200k_base (gpt-4o 계열) 로 fallback
                logger.debug("tiktoken.encoding_for_model(%s) 미등록, o200k_base 사용: %s",
                             self._model, e)
                enc = tiktoken.get_encoding("o200k_base")
            return len(enc.encode(text)), "tiktoken"
        except Exception as e:
            logger.debug("tiktoken 미설치/초기화 실패, chars/3 휴리스틱으로 폴백: %s", e)
            return super().count_tokens(text)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        stream: bool = True,
        thinking: Optional[dict] = None,
        tool_choice: Optional[str] = None,
    ) -> AsyncGenerator[ProviderEvent, None]:
        # Anthropic 메시지 포맷 → OpenAI 포맷 변환
        oai_messages = _convert_messages(messages, system)
        oai_tools = _convert_tools(tools) if tools else None

        body: dict[str, Any] = {
            "model": self._model,
            "messages": oai_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if oai_tools:
            body["tools"] = oai_tools
            # v0.11.19 — tool_choice 전달 (auto/required/none 또는 {"type":"function","function":{"name":...}})
            if tool_choice:
                tc = tool_choice
                if tc in ("auto", "required", "none"):
                    body["tool_choice"] = tc
                elif isinstance(tc, str) and tc not in ("auto", "required", "none"):
                    body["tool_choice"] = {"type": "function", "function": {"name": tc}}

        if stream:
            body["stream_options"] = {"include_usage": True}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        if stream:
            async for event in self._stream_request(body, headers):
                yield event
        else:
            event = await self._batch_request(body, headers)
            yield event

    async def _stream_request(
        self, body: dict, headers: dict
    ) -> AsyncGenerator[ProviderEvent, None]:
        current_tool_calls: dict[int, dict] = {}

        # Native `<tool_call>` text 파싱용 버퍼.
        # text_buf: 일반 text 누적. tool_call 시작 태그 매칭 가능 마진(_TOOL_CALL_OPEN_LEN)
        # 까지 buffer 유지 — 그 미만은 즉시 flush. tool_buf: <tool_call>...</tool_call>
        # 사이의 본문. 닫는 태그 만나면 _parse_native_tool_call 로 변환 후 TOOL_USE emit.
        text_buf = ""
        in_tool_call = False
        tool_buf = ""

        def _flush_text(force: bool = False) -> Optional[ProviderEvent]:
            """text_buf 의 안전한 부분만 flush. 끝부분 마진은 buffer 유지."""
            nonlocal text_buf
            if not text_buf:
                return None
            if force:
                ev = ProviderEvent(type=ProviderEventType.TEXT_DELTA, text=text_buf)
                text_buf = ""
                return ev
            # _TOOL_CALL_OPEN 의 부분 매칭 후보를 위해 마지막 N자 보류.
            if len(text_buf) <= _TOOL_CALL_OPEN_LEN:
                return None
            emit_chars = text_buf[:-_TOOL_CALL_OPEN_LEN]
            text_buf = text_buf[-_TOOL_CALL_OPEN_LEN:]
            return ProviderEvent(type=ProviderEventType.TEXT_DELTA, text=emit_chars)

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            async with client.stream("POST", self._base_url, json=body, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    raise ProviderError.from_status(response.status_code, error_body.decode())

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # usage 이벤트 (stream_options)
                    usage = data.get("usage")
                    if usage:
                        yield ProviderEvent(
                            type=ProviderEventType.USAGE,
                            input_tokens=usage.get("prompt_tokens", 0),
                            output_tokens=usage.get("completion_tokens", 0),
                        )

                    choices = data.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    finish_reason = choices[0].get("finish_reason")

                    # 텍스트 델타 — native <tool_call> 감지 + 파싱
                    content = delta.get("content")
                    if content:
                        # tool_call 본문 진행 중이면 tool_buf 에 누적
                        if in_tool_call:
                            tool_buf += content
                            close_idx = tool_buf.find(_TOOL_CALL_CLOSE)
                            if close_idx >= 0:
                                inner = tool_buf[:close_idx]
                                rest = tool_buf[close_idx + len(_TOOL_CALL_CLOSE):]
                                parsed = _parse_native_tool_call(inner)
                                if parsed is not None:
                                    yield ProviderEvent(
                                        type=ProviderEventType.TOOL_USE,
                                        tool_use_id=f"native_{uuid.uuid4().hex[:16]}",
                                        tool_name=parsed["name"],
                                        tool_input=parsed["arguments"],
                                    )
                                else:
                                    # 파싱 실패 — 원본 그대로 사용자에게 stream
                                    yield ProviderEvent(
                                        type=ProviderEventType.TEXT_DELTA,
                                        text=f"{_TOOL_CALL_OPEN}{inner}{_TOOL_CALL_CLOSE}",
                                    )
                                in_tool_call = False
                                tool_buf = ""
                                # 닫는 태그 이후 텍스트는 일반 흐름으로 재진입
                                if rest:
                                    text_buf += rest
                            # 닫는 태그 미발견 — 계속 누적
                        else:
                            text_buf += content
                            open_idx = text_buf.find(_TOOL_CALL_OPEN)
                            if open_idx >= 0:
                                # 시작 발견 — 이전 텍스트 flush, 이후를 tool_buf 로 전환
                                before = text_buf[:open_idx]
                                after = text_buf[open_idx + _TOOL_CALL_OPEN_LEN:]
                                if before:
                                    yield ProviderEvent(
                                        type=ProviderEventType.TEXT_DELTA, text=before,
                                    )
                                in_tool_call = True
                                tool_buf = after
                                text_buf = ""
                                # 같은 chunk 안에 close 가 같이 와있을 수도 → 즉시 처리
                                close_idx = tool_buf.find(_TOOL_CALL_CLOSE)
                                if close_idx >= 0:
                                    inner = tool_buf[:close_idx]
                                    rest = tool_buf[close_idx + len(_TOOL_CALL_CLOSE):]
                                    parsed = _parse_native_tool_call(inner)
                                    if parsed is not None:
                                        yield ProviderEvent(
                                            type=ProviderEventType.TOOL_USE,
                                            tool_use_id=f"native_{uuid.uuid4().hex[:16]}",
                                            tool_name=parsed["name"],
                                            tool_input=parsed["arguments"],
                                        )
                                    else:
                                        yield ProviderEvent(
                                            type=ProviderEventType.TEXT_DELTA,
                                            text=f"{_TOOL_CALL_OPEN}{inner}{_TOOL_CALL_CLOSE}",
                                        )
                                    in_tool_call = False
                                    tool_buf = ""
                                    if rest:
                                        text_buf += rest
                            else:
                                # tag 부분 매칭 후보(<tool_) 만 buffer, 나머지는 즉시 flush
                                ev = _flush_text()
                                if ev is not None:
                                    yield ev

                    # 도구 호출 델타 — native OpenAI tool_calls (vLLM hermes parser
                    # 켜진 경우 이쪽 경로로 옴). 두 경로 (native field + text XML) 가
                    # 동시에 활성화되지 않게 vLLM 이 정상 구성됐다고 가정 (둘 다 오면
                    # 중복 emit 가능 — 운영 시 hermes parser 활성화 권장 메시지 출력).
                    tool_calls = delta.get("tool_calls", [])
                    for tc in tool_calls:
                        idx = tc.get("index", 0)
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc.get("id", ""),
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": "",
                            }
                        else:
                            args = tc.get("function", {}).get("arguments", "")
                            current_tool_calls[idx]["arguments"] += args

                    if finish_reason:
                        # 미완 tool_call 본문이 있으면 fallback (close 태그 없이 stop)
                        if in_tool_call and tool_buf:
                            parsed = _parse_native_tool_call(tool_buf)
                            if parsed is not None:
                                yield ProviderEvent(
                                    type=ProviderEventType.TOOL_USE,
                                    tool_use_id=f"native_{uuid.uuid4().hex[:16]}",
                                    tool_name=parsed["name"],
                                    tool_input=parsed["arguments"],
                                )
                            else:
                                yield ProviderEvent(
                                    type=ProviderEventType.TEXT_DELTA,
                                    text=f"{_TOOL_CALL_OPEN}{tool_buf}",
                                )
                        elif text_buf:
                            yield ProviderEvent(
                                type=ProviderEventType.TEXT_DELTA, text=text_buf,
                            )
                        text_buf = ""
                        tool_buf = ""
                        in_tool_call = False

                        # 도구 호출 완료 시 emit (native field 경로)
                        for tc_data in current_tool_calls.values():
                            try:
                                parsed = json.loads(tc_data["arguments"]) if tc_data["arguments"] else {}
                            except json.JSONDecodeError:
                                parsed = {"raw": tc_data["arguments"]}
                            yield ProviderEvent(
                                type=ProviderEventType.TOOL_USE,
                                tool_use_id=tc_data["id"],
                                tool_name=tc_data["name"],
                                tool_input=parsed,
                            )
                        current_tool_calls.clear()

                        yield ProviderEvent(
                            type=ProviderEventType.STOP,
                            stop_reason=finish_reason,
                        )

    async def _batch_request(self, body: dict, headers: dict) -> ProviderEvent:
        body["stream"] = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            response = await client.post(self._base_url, json=body, headers=headers)
            if response.status_code != 200:
                raise ProviderError.from_status(response.status_code, response.text)

            data = response.json()
            choice = data["choices"][0]
            message = choice.get("message", {})
            usage = data.get("usage", {})

            text_content = message.get("content", "") or ""
            # native field tool_calls 가 비어있고 본문에 <tool_call> XML 있으면 추출.
            # batch path 는 stream 과 달리 chunk 없으니 단순 한 번 파싱.
            native_tool_calls = message.get("tool_calls") or []
            inline_tool_uses: list[dict] = []
            if not native_tool_calls and _TOOL_CALL_OPEN in text_content:
                cleaned_parts: list[str] = []
                cursor = 0
                while True:
                    open_idx = text_content.find(_TOOL_CALL_OPEN, cursor)
                    if open_idx < 0:
                        cleaned_parts.append(text_content[cursor:])
                        break
                    cleaned_parts.append(text_content[cursor:open_idx])
                    close_idx = text_content.find(_TOOL_CALL_CLOSE, open_idx + _TOOL_CALL_OPEN_LEN)
                    if close_idx < 0:
                        # close 태그 없음 → 잔여 그대로 fallback
                        cleaned_parts.append(text_content[open_idx:])
                        break
                    inner = text_content[open_idx + _TOOL_CALL_OPEN_LEN:close_idx]
                    parsed = _parse_native_tool_call(inner)
                    if parsed is not None:
                        inline_tool_uses.append({
                            "id": f"native_{uuid.uuid4().hex[:16]}",
                            "name": parsed["name"],
                            "input": parsed["arguments"],
                        })
                    else:
                        cleaned_parts.append(f"{_TOOL_CALL_OPEN}{inner}{_TOOL_CALL_CLOSE}")
                    cursor = close_idx + len(_TOOL_CALL_CLOSE)
                text_content = "".join(cleaned_parts)

            # batch 경로는 단일 ProviderEvent 반환 시그니처라 다중 TOOL_USE emit
                # 불가. inline_tool_uses 가 있으면 raw 에 첨부 — 호출부가 raw 에서
                # `__inline_tool_uses__` 를 읽어 처리. prod 흐름은 stream=True 라
                # 실질 영향 적음 (stream 경로는 정상 TOOL_USE event emit).
            raw_out = dict(data) if isinstance(data, dict) else {"__data__": data}
            if inline_tool_uses:
                raw_out["__inline_tool_uses__"] = inline_tool_uses
            return ProviderEvent(
                type=ProviderEventType.STOP,
                text=text_content,
                stop_reason=choice.get("finish_reason", ""),
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                raw=raw_out,
            )


def _convert_messages(messages: list[dict], system: Optional[str] = None) -> list[dict]:
    """Anthropic 메시지 포맷 → OpenAI 포맷.

    핵심 차이:
    - Anthropic assistant: content=[text, tool_use, ...]
    - OpenAI   assistant: content=text, tool_calls=[{id, type:"function", function:{name, arguments}}]

    - Anthropic user: content=[tool_result, ...]
    - OpenAI    tool: {role:"tool", tool_call_id, content}
    """
    oai_msgs = []
    if system:
        oai_msgs.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            oai_msgs.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            oai_msgs.append({"role": role, "content": str(content)})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    text_parts.append(str(block))
                    continue
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text", "")
                    if t:
                        text_parts.append(t)
                elif btype == "tool_use":
                    args = block.get("input") or {}
                    try:
                        args_str = json.dumps(args, ensure_ascii=False)
                    except Exception as e:
                        logger.debug("tool_use input JSON 직렬화 실패 (%s), 빈 객체로 fallback: %s",
                                     block.get("name", "?"), e)
                        args_str = "{}"
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": args_str,
                        },
                    })
                elif btype == "thinking":
                    # OpenAI는 thinking 블록 미지원 — 무시
                    continue
                else:
                    text_parts.append(str(block))

            assistant_msg: dict = {"role": "assistant"}
            assistant_msg["content"] = "\n".join(text_parts) if text_parts else None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            oai_msgs.append(assistant_msg)
            continue

        # user (또는 기타) — tool_result 블록은 tool 역할로 분리
        for block in content:
            if not isinstance(block, dict):
                oai_msgs.append({"role": role, "content": str(block)})
                continue
            btype = block.get("type")
            if btype == "tool_result":
                tc_content = block.get("content", "")
                # content가 list of blocks인 경우 문자열로 평탄화
                if isinstance(tc_content, list):
                    flat = []
                    for sub in tc_content:
                        if isinstance(sub, dict):
                            flat.append(sub.get("text", "") or str(sub))
                        else:
                            flat.append(str(sub))
                    tc_content = "\n".join(flat)
                oai_msgs.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": str(tc_content),
                })
            elif btype == "text":
                oai_msgs.append({"role": role, "content": block.get("text", "")})
            else:
                oai_msgs.append({"role": role, "content": str(block)})

    return oai_msgs


def _normalize_for_openai(schema: Any) -> Any:
    """JSON Schema → OpenAI Function calling 호환 정규화.

    v0.26.13 — Tavily / 일부 MCP 서버가 보내는 풍부한 schema 가 OpenAI 의
    ``invalid_function_parameters`` 400 을 유발하던 결함. Anthropic 은 관대하게
    수용하지만 OpenAI 는 type 배열 / anyOf-null / ``$ref`` 등을 거부한다.
    엔진 측에서 단방향 평탄화로 안전망을 얹는다.

    - ``"type": ["string", "null"]`` 같은 type 배열 → null 제거 후 단일 type
    - ``anyOf`` / ``oneOf`` 안에 null branch 만 빼면 단일 schema 가 되는 경우 평탄화
    - ``$ref`` 는 인라인 못 풀므로 drop (속성 자리 차지하지 않게)
    - 재귀적으로 dict / list 항목을 모두 처리

    의미 손실은 nullable 표현 → "필수 아닌 단일 type" 으로 약화되는 정도. OpenAI
    Function calling 은 어차피 nullable 을 수용 안 하므로 이 약화가 정상 통로.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            # 1) type 배열 → 단일 type (null 제거)
            if k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                out[k] = non_null[0] if len(non_null) == 1 else (non_null[0] if non_null else "string")
                continue
            # 2) anyOf / oneOf — null branch 만 빼면 단일이 되는 경우 평탄화
            if k in ("anyOf", "oneOf") and isinstance(v, list):
                non_null = [s for s in v if not (isinstance(s, dict) and s.get("type") == "null")]
                if len(non_null) == 1 and isinstance(non_null[0], dict):
                    # 단일 schema 의 키들을 부모로 끌어올림 (단, 같은 키가 이미 있으면 부모 우선)
                    flat = _normalize_for_openai(non_null[0])
                    if isinstance(flat, dict):
                        for fk, fv in flat.items():
                            out.setdefault(fk, fv)
                        continue
                out[k] = [_normalize_for_openai(s) for s in v]
                continue
            # 3) $ref 는 OpenAI 가 못 풂 → drop
            if k == "$ref":
                continue
            # 4) enum 이 dict 배열 (예: xgen-nodes 의 [{"value":"a","label":"A"}, ...])
            #    이면 primitive 만 추출. OpenAI 는 enum 항목으로 dict 거부.
            if k == "enum" and isinstance(v, list):
                flat_enum: list[Any] = []
                for item in v:
                    if isinstance(item, dict):
                        val = item.get("value")
                        if val is None:
                            val = item.get("id") or item.get("label")
                        if val is not None:
                            flat_enum.append(val)
                    else:
                        flat_enum.append(item)
                out[k] = flat_enum
                continue
            # 재귀
            out[k] = _normalize_for_openai(v)
        return out
    if isinstance(schema, list):
        return [_normalize_for_openai(s) for s in schema]
    return schema


def _convert_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool 정의 → OpenAI function 정의

    v0.26.2 — input_schema 가 ``{"type":"object"}`` 처럼 properties 누락 시
    OpenAI 가 ``"object schema missing properties"`` 로 400 거부함. input_schema
    가 단순한 케이스를 보정.
    v0.26.13 — Tavily 같은 MCP 도구의 type 배열 / anyOf-null / $ref 패턴 정규화.
    """
    oai_tools = []
    for tool in tools:
        params = dict(tool.get("input_schema") or {})
        # v0.26.13 — 정규화 (배열 type / nullable anyOf / $ref) 를 첫 단계에서.
        params = _normalize_for_openai(params)
        # OpenAI 호환 보정: type=object 인데 properties 누락이면 빈 dict 추가
        if params.get("type") == "object" and "properties" not in params:
            params["properties"] = {}
        oai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": params,
            },
        })
    return oai_tools
