"""xgen_sdk.image_generation — 모든 HTTP 는 httpx.MockTransport (네트워크 없음)."""
import asyncio
import base64
import hashlib
import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from xgen_sdk import llm_catalog
from xgen_sdk.comfyui.errors import ArgumentError, ComfyUITimeout
from xgen_sdk.image_generation import (
    ERROR_CODES,
    FALLBACK_MODELS,
    USER_MESSAGES,
    Credentials,
    ImageGenerationError,
    ImageRequest,
    capabilities_for,
    comfyui_adapter,
    comfyui_capabilities,
    comfyui_supports_edit,
    fetch_models,
    generate_image,
    is_image_model,
    list_models,
    open_api_key,
    resolve_credentials,
    seal_api_key,
    test_connection as probe_connection,
)
from xgen_sdk.image_generation import credentials as cred_module

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
OPENAI_KEY = "sk-test-openai-key-123456"
GEMINI_KEY = "AIzaTestGeminiKey1234567890"


def run(coro):
    return asyncio.run(coro)


def b64(data):
    return base64.b64encode(data).decode()


class Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)

    @property
    def transport(self):
        return httpx.MockTransport(self)


# ─── 판별 · llm_catalog ──────────────────────────────────────────
@pytest.mark.parametrize("mid", [
    "gpt-image-1", "gpt-image-1.5", "gpt-image-1-mini", "chatgpt-image-latest",
    "gemini-2.5-flash-image", "gemini-3-pro-image-preview", "gemini-2.0-flash-preview-image-generation",
    "imagen-4.0-generate-001", "dall-e-3",
])
def test_is_image_model_true(mid):
    assert is_image_model(mid)


@pytest.mark.parametrize("mid", ["gpt-4o", "gpt-5.4-mini", "o3", "gemini-2.5-flash", "gemini-3-pro", "claude-opus-4-7", "", None])
def test_is_image_model_false(mid):
    assert not is_image_model(mid)


def test_llm_catalog_chat_excludes_openai_image_models():
    models = [{"value": v, "label": v} for v in ("gpt-4o", "gpt-image-1", "chatgpt-image-latest", "gpt-5", "dall-e-3")]
    assert [m["value"] for m in llm_catalog._filter_chat("openai", models)] == ["gpt-4o", "gpt-5"]


def test_llm_catalog_gemini_fetch_excludes_image_models(monkeypatch):
    body = {"models": [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3-pro-image-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/imagen-4.0-generate-001", "supportedGenerationMethods": ["predict"]},
    ]}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    real = httpx.Client
    monkeypatch.setattr(llm_catalog.httpx, "Client", lambda **kw: real(transport=transport, **kw))
    assert [m["value"] for m in llm_catalog._fetch_gemini("k")] == ["gemini-2.5-flash"]
    assert [m["value"] for m in llm_catalog._filter_vision("gemini", [{"value": "gemini-2.5-flash-image"}])] == []


# ─── 오류 문구 ────────────────────────────────────────────────────
def test_user_messages_are_korean_and_brand_free():
    assert set(USER_MESSAGES) == set(ERROR_CODES) == {
        "connection", "auth", "invalid_input", "safety_blocked", "timeout", "no_output", "quota",
    }
    for msg in USER_MESSAGES.values():
        for brand in ("OpenAI", "Gemini", "Google", "ComfyUI", "Nano", "GPT"):
            assert brand.lower() not in msg.lower()
        assert "이미지" in msg or "안전" in msg


# ─── OpenAI ──────────────────────────────────────────────────────
def test_openai_generate_posts_json_and_parses_usage():
    def handler(request):
        return httpx.Response(200, json={
            "created": 1, "output_format": "png",
            "data": [{"b64_json": b64(PNG)}, {"b64_json": b64(JPEG), "revised_prompt": "a cat, photo"}],
            "usage": {"input_tokens": 12, "output_tokens": 4160, "input_tokens_details": {"text_tokens": 12},
                      "output_tokens_details": {"image_tokens": 4160}},
        })

    rec = Recorder(handler)
    creds = Credentials(api_key=OPENAI_KEY, base_url="https://api.openai.com/v1", source="llm")
    result = run(generate_image("openai", ImageRequest(prompt="a cat", n=2, size="1024x1024", quality="high"),
                                model="gpt-image-1", credentials=creds, transport=rec.transport))
    req = rec.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/images/generations"
    assert req.headers["authorization"] == f"Bearer {OPENAI_KEY}"
    assert json.loads(req.content) == {"model": "gpt-image-1", "prompt": "a cat", "n": 2, "size": "1024x1024", "quality": "high"}
    assert [i.mime for i in result.images] == ["image/png", "image/jpeg"]
    assert result.images[1].revised_prompt == "a cat, photo"
    assert result.usage.to_dict() == {"input_tokens": 12, "output_tokens": 4160, "image_tokens": 4160}
    assert result.operation == "generate" and result.provider == "openai" and result.model == "gpt-image-1"
    # 계약의 dict 모양으로도 읽힌다
    assert result["images"][0]["data"] == PNG and result["usage"]["input_tokens"] == 12


def test_openai_edit_sends_multipart_with_all_references():
    rec = Recorder(lambda r: httpx.Response(200, json={"data": [{"b64_json": b64(PNG)}]}))
    request = ImageRequest(prompt="combine", reference_images=[{"data": PNG, "mime": "image/png"}, {"data": JPEG, "mime": "image/jpeg"}])
    result = run(generate_image("openai", request, model="gpt-image-1", credentials={"api_key": OPENAI_KEY, "base_url": "https://proxy.local"},
                                transport=rec.transport))
    req = rec.requests[0]
    assert str(req.url) == "https://proxy.local/v1/images/edits"
    body = req.content
    assert body.count(b'name="image[]"') == 2
    assert b'name="model"' in body and b"gpt-image-1" in body and b'name="prompt"' in body
    assert result.operation == "edit" and result.usage is None


@pytest.mark.parametrize("status,body,code", [
    (401, {"error": {"message": f"Incorrect API key provided: {OPENAI_KEY}", "code": "invalid_api_key"}}, "auth"),
    (429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}, "quota"),
    (400, {"error": {"message": "Your request was rejected by the safety system.", "code": "moderation_blocked"}}, "safety_blocked"),
    (400, {"error": {"message": "Invalid size", "param": "size"}}, "invalid_input"),
    (500, {"error": {"message": "server error"}}, "connection"),
])
def test_openai_http_errors(status, body, code):
    rec = Recorder(lambda r: httpx.Response(status, json=body))
    with pytest.raises(ImageGenerationError) as info:
        run(generate_image("openai", ImageRequest(prompt="x"), model="gpt-image-1",
                           credentials={"api_key": OPENAI_KEY}, transport=rec.transport))
    err = info.value
    assert err.code == code and err.message == USER_MESSAGES[code] and err.status_code == status
    assert OPENAI_KEY not in str(err.detail) and OPENAI_KEY not in err.message


def test_openai_timeout_and_connection():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    for handler, code in ((timeout, "timeout"), (refused, "connection")):
        with pytest.raises(ImageGenerationError) as info:
            run(generate_image("openai", ImageRequest(prompt="x"), model="gpt-image-1",
                               credentials={"api_key": OPENAI_KEY}, transport=httpx.MockTransport(handler)))
        assert info.value.code == code


def test_openai_no_images_and_no_key():
    rec = Recorder(lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(ImageGenerationError) as info:
        run(generate_image("openai", ImageRequest(prompt="x"), model="gpt-image-1", credentials={"api_key": OPENAI_KEY}, transport=rec.transport))
    assert info.value.code == "no_output"
    with pytest.raises(ImageGenerationError) as info:
        run(generate_image("openai", ImageRequest(prompt="x"), model="gpt-image-1", credentials=None, transport=rec.transport))
    assert info.value.code == "auth" and len(rec.requests) == 1


def test_validate_request_against_capabilities():
    creds = {"api_key": OPENAI_KEY}
    cases = [
        (ImageRequest(prompt="x", n=11), None),
        (ImageRequest(prompt="x", size="999x999"), None),
        (ImageRequest(prompt="x", quality="ultra"), None),
        (ImageRequest(prompt="x", reference_images=[{"data": PNG}]), {"generate": True, "edit": False, "sizes": [], "qualities": [], "max_n": 1}),
        (ImageRequest(prompt="x", reference_images=[{"data": PNG}] * 4), {"edit": True, "max_reference_images": 3, "max_n": 1}),
        (ImageRequest(prompt=" "), None),
    ]
    for request, caps in cases:
        with pytest.raises(ImageGenerationError) as info:
            run(generate_image("openai", request, model="gpt-image-1", credentials=creds, capabilities=caps,
                               transport=httpx.MockTransport(lambda r: httpx.Response(500))))
        assert info.value.code == "invalid_input"
    with pytest.raises(ImageGenerationError) as info:
        run(generate_image("comfyui", ImageRequest(prompt="x"), model="e", credentials=None))
    assert info.value.code == "invalid_input"


# ─── Gemini ──────────────────────────────────────────────────────
def _gemini_ok(request):
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "Here you go"}, {"inlineData": {"mimeType": "image/png", "data": b64(PNG)}}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 1300,
                          "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1290}, {"modality": "TEXT", "tokenCount": 10}]},
    })


def test_gemini_edit_sends_reference_parts_and_header_key():
    rec = Recorder(_gemini_ok)
    request = ImageRequest(prompt="make it blue", n=2, size="16:9", quality="2K", reference_images=[{"data": JPEG, "mime": "image/jpeg"}])
    result = run(generate_image("gemini", request, model="gemini-3-pro-image-preview",
                                credentials=Credentials(api_key=GEMINI_KEY, base_url=None, source="custom"), transport=rec.transport))
    assert len(rec.requests) == 2  # n 장 = 요청 n 번
    req = rec.requests[0]
    assert str(req.url) == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent"
    assert GEMINI_KEY not in str(req.url) and req.headers["x-goog-api-key"] == GEMINI_KEY
    body = json.loads(req.content)
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"inlineData": {"mimeType": "image/jpeg", "data": b64(JPEG)}}
    assert parts[-1] == {"text": "make it blue"}
    assert body["generationConfig"] == {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": "16:9", "imageSize": "2K"}}
    assert len(result.images) == 2 and result.operation == "edit"
    assert result.usage.to_dict() == {"input_tokens": 20, "output_tokens": 2600, "image_tokens": 2580}
    assert result.metadata["text"]


def test_gemini_2_5_has_no_quality_option():
    caps = capabilities_for("gemini", "gemini-2.5-flash-image")
    assert caps.edit and caps.qualities == [] and "16:9" in caps.sizes and caps.max_reference_images == 3
    with pytest.raises(ImageGenerationError):
        run(generate_image("gemini", ImageRequest(prompt="x", quality="4K"), model="gemini-2.5-flash-image",
                           credentials={"api_key": GEMINI_KEY}, transport=httpx.MockTransport(_gemini_ok)))


@pytest.mark.parametrize("payload,code", [
    ({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, "safety_blocked"),
    ({"candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}]}, "safety_blocked"),
    ({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "I can only describe it."}]}}]}, "no_output"),
])
def test_gemini_blocked_or_empty(payload, code):
    with pytest.raises(ImageGenerationError) as info:
        run(generate_image("gemini", ImageRequest(prompt="x"), model="gemini-2.5-flash-image",
                           credentials={"api_key": GEMINI_KEY}, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))))
    assert info.value.code == code


def test_gemini_invalid_key_is_auth_and_partial_success_keeps_images():
    bad = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}
    with pytest.raises(ImageGenerationError) as info:
        run(generate_image("gemini", ImageRequest(prompt="x"), model="gemini-2.5-flash-image",
                           credentials={"api_key": GEMINI_KEY}, transport=httpx.MockTransport(lambda r: httpx.Response(400, json=bad))))
    assert info.value.code == "auth"

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}})
        return _gemini_ok(request)

    result = run(generate_image("gemini", ImageRequest(prompt="x", n=2), model="gemini-2.5-flash-image",
                                credentials={"api_key": GEMINI_KEY}, transport=httpx.MockTransport(flaky)))
    assert len(result.images) == 1 and result.metadata["partial_errors"] == ["quota"]


# ─── 모델 목록 ────────────────────────────────────────────────────
def test_openai_list_models_filters_and_recommends_one():
    body = {"data": [{"id": i} for i in ("gpt-4o", "gpt-image-1", "gpt-image-1-mini", "dall-e-3", "gpt-image-1.5", "whisper-1")]}
    rec = Recorder(lambda r: httpx.Response(200, json=body))
    models = run(list_models("openai", {"api_key": OPENAI_KEY}, transport=rec.transport))
    assert str(rec.requests[0].url) == "https://api.openai.com/v1/models"
    assert [m.model_id for m in models] == ["gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5"]
    assert [m.model_id for m in models if m.recommended] == ["gpt-image-1"]
    assert all(m.source == "sync" and m.capabilities.edit for m in models)
    assert models[0].to_dict()["capabilities"] == {
        "generate": True, "edit": True, "sizes": ["auto", "1024x1024", "1536x1024", "1024x1536"],
        "qualities": ["auto", "low", "medium", "high"], "max_n": 10, "max_reference_images": 16,
    }


def test_gemini_list_models_paginates_and_filters():
    pages = {
        None: {"models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-flash-image", "displayName": "Nano Banana", "supportedGenerationMethods": ["generateContent"]},
        ], "nextPageToken": "p2"},
        "p2": {"models": [
            {"name": "models/imagen-4.0-generate-001", "supportedGenerationMethods": ["predict"]},
            {"name": "models/gemini-3-pro-image-preview", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        ]},
    }
    rec = Recorder(lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken")]))
    models = run(fetch_models("gemini", {"api_key": GEMINI_KEY}, transport=rec.transport))
    assert [m.model_id for m in models] == ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"]
    assert models[0].display_name == "Nano Banana" and models[0].recommended and not models[1].recommended
    assert models[1].capabilities.qualities == ["1K", "2K", "4K"] and models[1].capabilities.max_reference_images == 14
    assert all(GEMINI_KEY not in str(r.url) for r in rec.requests)


def test_list_models_falls_back_without_key_or_on_failure():
    for creds, transport in ((None, None), ({"api_key": OPENAI_KEY}, httpx.MockTransport(lambda r: httpx.Response(401, json={})))):
        models = run(list_models("openai", creds, transport=transport))
        assert [m.model_id for m in models] == [row["model_id"] for row in FALLBACK_MODELS["openai"]]
        assert all(m.source == "fallback" for m in models) and sum(m.recommended for m in models) == 1
    gem = run(list_models("gemini", {"api_key": GEMINI_KEY}, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"models": []}))))
    assert [m.model_id for m in gem] == ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"]
    assert run(list_models("comfyui", None)) == []


def test_connection_probe_reports_user_message():
    bad = run(probe_connection("openai", {"api_key": OPENAI_KEY}, transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))))
    assert bad == {"ok": False, "model_count": 0, "code": "auth", "message": USER_MESSAGES["auth"]}
    ok = run(probe_connection("openai", {"api_key": OPENAI_KEY},
                              transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "gpt-image-1"}]}))))
    assert ok["ok"] and ok["model_count"] == 1


# ─── 자격증명 ─────────────────────────────────────────────────────
class FakeDB:
    def __init__(self, row=None, fail=False):
        self.row, self.fail, self.queries = row, fail, []

    def execute_query(self, sql, params=None):
        self.queries.append((sql, params))
        if self.fail:
            raise RuntimeError("relation does not exist")
        return [self.row] if self.row else []


class FakeComposer:
    def __init__(self, values):
        self.values = values

    def get_config_by_name(self, name):
        if name not in self.values:
            raise KeyError(name)
        return type("Cfg", (), {"value": self.values[name]})()


def _core_encrypt(plaintext):
    """xgen-core service/db_connections/crypto.encrypt_credential 와 같은 방식."""
    secret = os.getenv("DB_CREDENTIAL_SECRET", "xgen-db-credential-secret-change-in-production")
    iv = os.urandom(12)
    blob = iv + AESGCM(hashlib.sha256(secret.encode()).digest()).encrypt(iv, plaintext.encode(), None)
    return base64.urlsafe_b64encode(blob).decode()


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_API_BASE_URL", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm_catalog, "_registered_composer", None)


def test_custom_key_round_trip_and_core_formats(clean_env):
    sealed = seal_api_key(OPENAI_KEY)
    assert OPENAI_KEY not in sealed and open_api_key(sealed) == OPENAI_KEY
    assert open_api_key(_core_encrypt(OPENAI_KEY)) == OPENAI_KEY                         # 키 문자열 그대로
    assert open_api_key(_core_encrypt(json.dumps({"token": OPENAI_KEY}))) == OPENAI_KEY   # ComfyUI 비밀 모양
    db = FakeDB({"provider_id": "openai", "credential_mode": "custom", "secret_ciphertext": sealed, "base_url": "https://gw.local/v1"})
    creds = resolve_credentials("openai", db, composer=FakeComposer({"OPENAI_API_KEY": "sk-llm-key-0000000"}))
    assert (creds.api_key, creds.base_url, creds.source) == (OPENAI_KEY, "https://gw.local/v1", "custom")
    assert db.queries[0][1] == ("openai",)
    assert OPENAI_KEY not in repr(creds) and OPENAI_KEY not in json.dumps(creds.to_dict())
    assert creds["source"] == "custom"


def test_llm_key_is_default(clean_env, monkeypatch):
    composer = FakeComposer({"OPENAI_API_KEY": " sk-llm-key-0000000 ", "OPENAI_API_BASE_URL": "https://llm-proxy/v1"})
    for row in (None, {"credential_mode": "llm", "secret_ciphertext": None}, {"credential_mode": "custom", "secret_ciphertext": ""}):
        creds = resolve_credentials("openai", FakeDB(row), composer=composer)
        assert (creds.api_key, creds.base_url, creds.source) == ("sk-llm-key-0000000", "https://llm-proxy/v1", "llm")
    # 표가 없어도(부팅 동기화 전) LLM 키로
    assert resolve_credentials("openai", FakeDB(fail=True), composer=composer).source == "llm"
    # 등록된 composer · 환경변수 순
    monkeypatch.setattr(llm_catalog, "_registered_composer", FakeComposer({"GEMINI_API_KEY": GEMINI_KEY}))
    gem = resolve_credentials("gemini", None)
    assert (gem.api_key, gem.source, gem.base_url) == (GEMINI_KEY, "llm", "https://generativelanguage.googleapis.com/v1beta")
    monkeypatch.setattr(llm_catalog, "_registered_composer", None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-0000000")
    env = resolve_credentials("openai", None)
    assert (env.api_key, env.base_url, env.source) == ("sk-env-key-0000000", "https://api.openai.com/v1", "llm")


def test_no_key_unreadable_and_comfyui(clean_env):
    none = resolve_credentials("gemini", FakeDB())
    assert none.source == "none" and not none.has_key
    bad = resolve_credentials("openai", FakeDB({"credential_mode": "custom", "secret_ciphertext": "not-a-cipher"}),
                              composer=FakeComposer({"OPENAI_API_KEY": "sk-llm-key-0000000"}))
    assert (bad.source, bad.api_key, bad.error) == ("none", None, "secret_unreadable")
    assert resolve_credentials("comfyui", FakeDB()).to_dict() == {"source": "none", "base_url": None, "has_key": False, "error": None}
    calls = []
    creds = resolve_credentials("openai", None, row={"credential_mode": "custom", "secret_ciphertext": "abc"},
                                decrypt=lambda c: calls.append(c) or OPENAI_KEY)
    assert creds.api_key == OPENAI_KEY and calls == ["abc"]


def test_custom_secret_survives_other_db_secret(clean_env, monkeypatch):
    sealed = seal_api_key(OPENAI_KEY)
    monkeypatch.setenv("DB_CREDENTIAL_SECRET", "rotated")
    with pytest.raises(cred_module.SecretUnreadable):
        open_api_key(sealed)


# ─── ComfyUI ─────────────────────────────────────────────────────
GRAPH = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}}, "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
MAPPING = {"version": 1, "outputs": ["2"], "params": [
    {"name": "prompt", "type": "string", "required": True, "targets": [{"node_id": "1", "input": "text"}]},
]}


def test_comfyui_v1_mapping_has_no_edit():
    assert not comfyui_supports_edit(MAPPING)
    assert comfyui_capabilities(MAPPING).to_dict() == {
        "generate": True, "edit": False, "sizes": [], "qualities": [], "max_n": 1, "max_reference_images": 0,
    }
    with pytest.raises(ImageGenerationError) as info:
        run(comfyui_adapter.generate(None, GRAPH, MAPPING, ImageRequest(prompt="x", reference_images=[{"data": PNG}])))
    assert info.value.code == "invalid_input" and "편집" in info.value.message


def test_comfyui_wraps_run_workflow(monkeypatch):
    seen = {}

    async def fake_run(client, graph, *, output_node_ids, timeout_s, max_files, max_bytes):
        seen.update(graph=graph, outputs=list(output_node_ids), timeout_s=timeout_s)
        return {"prompt_id": "p1", "duration_ms": 1234, "files": [
            {"node_id": "2", "filename": "xgen_00001_.png", "content_type": "image/png", "data": PNG},
        ]}

    monkeypatch.setattr(comfyui_adapter, "run_workflow", fake_run)
    result = run(comfyui_adapter.generate(object(), GRAPH, MAPPING, ImageRequest(prompt="a fox"), timeout_s=60, entry_key="sdxl"))
    assert seen["graph"]["1"]["inputs"]["text"] == "a fox" and GRAPH["1"]["inputs"]["text"] == "old"
    assert seen["outputs"] == ["2"] and seen["timeout_s"] == 60
    assert result.provider == "comfyui" and result.model == "sdxl" and result.latency_ms == 1234
    assert result.images[0].filename == "xgen_00001_.png" and result.images[0].mime == "image/png"
    assert result.metadata == {"prompt_id": "p1", "files": [{"node_id": "2", "filename": "xgen_00001_.png"}]}


def test_comfyui_errors_map_to_common_codes(monkeypatch):
    async def slow(*a, **kw):
        raise ComfyUITimeout("이미지 생성 서버가 정해진 시간 안에 끝내지 않았습니다.", detail="timeout after 5s")

    monkeypatch.setattr(comfyui_adapter, "run_workflow", slow)
    with pytest.raises(ImageGenerationError) as info:
        run(comfyui_adapter.generate(object(), GRAPH, MAPPING, arguments={"prompt": "x"}))
    assert info.value.code == "timeout" and info.value.detail["comfyui_code"] == "timeout"
    assert isinstance(info.value.__cause__, ComfyUITimeout)
    with pytest.raises(ImageGenerationError) as info:
        run(comfyui_adapter.generate(object(), GRAPH, MAPPING, arguments={"nope": 1}))
    assert info.value.code == "invalid_input" and "Unknown argument" in info.value.detail
    assert isinstance(info.value.__cause__, ArgumentError)
