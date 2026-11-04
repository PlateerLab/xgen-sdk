"""평가 호출이 temperature 를 거부하는 모델에서도 끝나는지."""
import json

import httpx
import pytest

from xgen_sdk.quality_judge import llm_judge


def _install(monkeypatch, handler):
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_judge.httpx, "Client", client)


def test_retries_without_temperature_when_rejected(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"param": "temperature", "code": "unsupported_value"}})
        return httpx.Response(200, json={"ok": True})

    _install(monkeypatch, handler)
    data = llm_judge._post_judge_request("https://x/v1", {"model": "m", "temperature": 0.0}, {})
    assert data == {"ok": True}
    assert ["temperature" in b for b in seen] == [True, False]


def test_keeps_temperature_when_accepted(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    _install(monkeypatch, handler)
    llm_judge._post_judge_request("https://x/v1", {"model": "m", "temperature": 0.0}, {})
    assert len(seen) == 1 and seen[0]["temperature"] == 0.0


def test_other_400_is_not_retried(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"param": "model", "code": "model_not_found"}})

    _install(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        llm_judge._post_judge_request("https://x/v1", {"model": "m", "temperature": 0.0}, {})
    assert len(seen) == 1


def test_still_fails_when_retry_fails(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "temperature is not supported"}})

    _install(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        llm_judge._post_judge_request("https://x/v1", {"model": "m", "temperature": 0.0}, {})
