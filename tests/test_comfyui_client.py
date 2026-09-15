"""ComfyUI 클라이언트 — httpx.MockTransport 로 모든 경로.

응답 본문은 실제 ComfyUI 0.35.0 에서 받은 모양(tests/fixtures/comfyui/)이다.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from xgen_sdk.comfyui import (
    ComfyUIAuthError,
    ComfyUIClient,
    ComfyUIConnectionError,
    ComfyUIExecutionError,
    ComfyUIOutputError,
    ComfyUIPromptError,
    ComfyUITimeout,
    content_type_for,
    run_workflow,
)
from xgen_sdk.comfyui import client as client_module

FIXTURES = Path(__file__).parent / "fixtures" / "comfyui"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SECRET = "Bearer super-secret-token"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


class Recorder:
    """요청을 기록하고 경로별 처리기로 답한다."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    async def __call__(self, request: httpx.Request):
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        handler = self.routes.get(key)
        if handler is None:
            for pattern, candidate in self.routes.items():
                if pattern.endswith("*") and key.startswith(pattern[:-1]):
                    handler = candidate
                    break
        if handler is None:
            return httpx.Response(404, text="404: Not Found")
        result = handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def calls(self, key):
        return [r for r in self.requests if f"{r.method} {r.url.path}" == key]


def make(routes, **kwargs):
    recorder = Recorder(routes)
    base = kwargs.pop("base_url", "http://comfy.local:8188")
    client = ComfyUIClient(base, transport=httpx.MockTransport(recorder), **kwargs)
    return client, recorder


def jsonr(body, status=200):
    return lambda request: httpx.Response(status, json=body)


# ─── 생성 ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    ["ftp://comfy:21", "comfy.local:8188", "", "http://", "http://user:pw@comfy:8188", "http://comfy:8188/?x=1"],
)
def test_base_url_rules(url):
    with pytest.raises(ComfyUIConnectionError) as info:
        ComfyUIClient(url)
    assert info.value.code == "connection"
    assert "pw" not in info.value.message


def test_client_does_not_trust_env_or_follow_redirects_and_hides_headers():
    client = ComfyUIClient("https://comfy.example.com/", headers={"Authorization": SECRET})
    try:
        assert client.base_url == "https://comfy.example.com"
        assert client._client.trust_env is False
        assert client._client.follow_redirects is False
        assert "secret" not in repr(client)
    finally:
        run(client.aclose())


def test_headers_must_be_strings():
    with pytest.raises(ComfyUIConnectionError):
        ComfyUIClient("http://comfy:8188", headers={"X-Token": 123})


def test_base_path_prefix_and_headers_are_sent():
    async def go():
        client, rec = make({"GET /proxy/comfy/system_stats": jsonr(_load("system_stats.json"))},
                           base_url="http://gw.local/proxy/comfy/", headers={"Authorization": SECRET})
        async with client:
            stats = await client.system_stats()
        return stats, rec

    stats, rec = run(go())
    assert stats["system"]["comfyui_version"] == "0.35.0"
    assert stats["devices"][0]["type"] == "cpu"
    assert rec.requests[0].headers["authorization"] == SECRET


# ─── 공통 오류 ────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors(status):
    async def go():
        client, _ = make({"GET /system_stats": jsonr({"error": "no"}, status)}, headers={"Authorization": SECRET})
        async with client:
            await client.system_stats()

    with pytest.raises(ComfyUIAuthError) as info:
        run(go())
    assert info.value.code == "auth"
    assert "secret" not in f"{info.value.message} {info.value.detail}"


def test_redirect_is_not_followed():
    async def go():
        client, rec = make({"GET /system_stats": lambda r: httpx.Response(302, headers={"location": "http://evil/"})})
        async with client:
            try:
                await client.system_stats()
            finally:
                assert len(rec.requests) == 1

    with pytest.raises(ComfyUIConnectionError):
        run(go())


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(200, text="<html>not comfy</html>"),
        lambda r: httpx.Response(500, text="Server got itself in trouble"),
        lambda r: httpx.Response(404, text="404: Not Found"),
        lambda r: httpx.Response(200, json={"no": "system"}),
    ],
)
def test_not_comfyui_responses(handler):
    async def go():
        client, _ = make({"GET /system_stats": handler})
        async with client:
            await client.system_stats()

    with pytest.raises(ComfyUIConnectionError) as info:
        run(go())
    assert info.value.detail


def test_transport_errors_map_to_connection_and_timeout():
    def refuse(request):
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    async def go(handler):
        client, _ = make({"GET /system_stats": handler})
        async with client:
            await client.system_stats()

    with pytest.raises(ComfyUIConnectionError) as info:
        run(go(refuse))
    assert info.value.code == "connection" and "ConnectError" in info.value.detail
    with pytest.raises(ComfyUITimeout) as info:
        run(go(slow))
    assert info.value.code == "timeout"


# ─── object_info ─────────────────────────────────────────────────
def test_object_info_requested_classes_only():
    sample = _load("object_info_sample.json")

    def one(request):
        name = request.url.path.rsplit("/", 1)[1]
        from urllib.parse import unquote
        name = unquote(name)
        if name == "Gone404":
            return httpx.Response(404)
        return httpx.Response(200, json={name: sample[name]} if name in sample else {})

    async def go():
        client, rec = make({"GET /object_info/*": one})
        async with client:
            out = await client.object_info(["KSampler", "NoSuchNodeXYZ", "KSampler", " ", "Gone404", "Save Image|x"])
        return out, rec

    out, rec = run(go())
    assert list(out) == ["KSampler"]
    assert out["KSampler"]["output_node"] is False
    paths = sorted(r.url.raw_path.decode() for r in rec.requests)
    assert paths == sorted([
        "/object_info/KSampler", "/object_info/NoSuchNodeXYZ", "/object_info/Gone404", "/object_info/Save%20Image%7Cx",
    ])


def test_object_info_all_and_empty_request():
    async def go():
        client, rec = make({"GET /object_info": jsonr({"SaveImage": {"output_node": True}})})
        async with client:
            everything = await client.object_info()
            nothing = await client.object_info([])
        return everything, nothing, rec

    everything, nothing, rec = run(go())
    assert everything == {"SaveImage": {"output_node": True}}
    assert nothing == {} and len(rec.requests) == 1


# ─── submit ──────────────────────────────────────────────────────
GRAPH = {
    "1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 48, "batch_size": 1, "color": 0}},
    "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "xgen", "images": ["1", 0]}},
}


def test_submit_success_sends_prompt_and_ids():
    async def go():
        client, rec = make({"POST /prompt": jsonr({"prompt_id": "server-id", "number": 3, "node_errors": {}})})
        async with client:
            first = await client.submit(GRAPH, client_id="me", prompt_id="0b6c6b0e-4c1f-4bb4-9d57-3bb8e0a8ad0d")
            second = await client.submit(GRAPH)
        return first, second, rec

    first, second, rec = run(go())
    assert first == "server-id" and second == "server-id"
    body = json.loads(rec.requests[0].content)
    assert body == {"prompt": GRAPH, "client_id": "me", "prompt_id": "0b6c6b0e-4c1f-4bb4-9d57-3bb8e0a8ad0d"}
    body2 = json.loads(rec.requests[1].content)
    assert "prompt_id" not in body2 and body2["client_id"].startswith("xgen-")


def test_submit_node_errors_real_shape():
    async def go():
        client, _ = make({"POST /prompt": jsonr(_load("prompt_error_node_errors.json"), 400)})
        async with client:
            await client.submit(GRAPH)

    with pytest.raises(ComfyUIPromptError) as info:
        run(go())
    err = info.value
    assert err.code == "prompt_invalid"
    assert err.error_type == "prompt_outputs_failed_validation"
    assert err.message == "ComfyUI가 워크플로우 입력값을 받아들이지 않았습니다."
    assert err.node_errors == [
        {"node_id": "1", "class_type": "EmptyImage", "input": "width",
         "message": "Failed to convert an input value to a INT value (width, abc, invalid literal for int() with base 10: 'abc')"},
        {"node_id": "1", "class_type": "EmptyImage", "input": "height",
         "message": "Value 99999 bigger than max of 16384 (height)"},
    ]
    assert err.detail["error"]["type"] == "prompt_outputs_failed_validation"


def test_submit_missing_node_and_no_outputs():
    async def go(body):
        client, _ = make({"POST /prompt": jsonr(body, 400)})
        async with client:
            await client.submit(GRAPH)

    with pytest.raises(ComfyUIPromptError) as info:
        run(go(_load("prompt_error_missing_node.json")))
    assert info.value.error_type == "missing_node_type"
    assert "NoSuchNodeXYZ" in info.value.message
    assert info.value.node_errors == [{
        "node_id": "1", "class_type": "NoSuchNodeXYZ", "input": None,
        "message": "Node 'NoSuchNodeXYZ' not found. The custom node may not be installed.",
    }]
    with pytest.raises(ComfyUIPromptError) as info:
        run(go(_load("prompt_error_no_outputs.json")))
    assert info.value.error_type == "prompt_no_outputs"
    assert info.value.node_errors == []


def test_submit_unreadable_400_and_missing_prompt_id():
    async def go(handler):
        client, _ = make({"POST /prompt": handler})
        async with client:
            await client.submit(GRAPH)

    with pytest.raises(ComfyUIPromptError):
        run(go(lambda r: httpx.Response(400, text="bad")))
    with pytest.raises(ComfyUIConnectionError):
        run(go(jsonr({"number": 1})))


# ─── wait ────────────────────────────────────────────────────────
def _history_sequence(bodies):
    state = {"i": 0}

    def handler(request):
        body = bodies[min(state["i"], len(bodies) - 1)]
        state["i"] += 1
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, json=body)

    return handler


SUCCESS = _load("history_success.json")
SUCCESS_ID = next(iter(SUCCESS))


def test_wait_polls_until_success():
    async def go():
        client, rec = make({f"GET /history/{SUCCESS_ID}": _history_sequence([{}, {}, SUCCESS])})
        async with client:
            entry = await client.wait(SUCCESS_ID, timeout_s=5, poll_interval=0.01)
        return entry, rec

    entry, rec = run(go())
    assert entry["status"]["status_str"] == "success"
    assert len(rec.requests) == 3


def test_wait_execution_error_real_shape():
    body = _load("history_execution_error.json")
    pid = next(iter(body))

    async def go():
        client, _ = make({f"GET /history/{pid}": jsonr(body)})
        async with client:
            await client.wait(pid, timeout_s=5, poll_interval=0.01)

    with pytest.raises(ComfyUIExecutionError) as info:
        run(go())
    err = info.value
    assert err.code == "execution" and err.node_id == "2" and err.node_type == "ImageToMask"
    assert not err.interrupted
    assert "ImageToMask" in err.message
    assert "IndexError: index 3 is out of bounds" in err.detail
    assert "Traceback" not in err.detail and "nodes_mask.py" not in err.detail


def test_wait_interrupted():
    body = {"p": {"outputs": {}, "status": {"status_str": "error", "completed": False, "messages": [
        ["execution_start", {"prompt_id": "p"}],
        ["execution_interrupted", {"prompt_id": "p", "node_id": "5", "node_type": "KSampler", "executed": []}],
    ]}}}

    async def go():
        client, _ = make({"GET /history/p": jsonr(body)})
        async with client:
            await client.wait("p", timeout_s=5, poll_interval=0.01)

    with pytest.raises(ComfyUIExecutionError) as info:
        run(go())
    assert info.value.interrupted is True and info.value.node_type == "KSampler"


def test_wait_timeout():
    async def go():
        client, rec = make({"GET /history/p": jsonr({})})
        async with client:
            try:
                await client.wait("p", timeout_s=0.2, poll_interval=0.05)
            finally:
                assert len(rec.requests) >= 2

    with pytest.raises(ComfyUITimeout) as info:
        run(go())
    assert info.value.code == "timeout"


def test_wait_tolerates_transient_failures_but_not_many():
    def refuse():
        return httpx.ConnectError("refused")

    async def go(bodies):
        client, _ = make({f"GET /history/{SUCCESS_ID}": _history_sequence(bodies)})
        async with client:
            return await client.wait(SUCCESS_ID, timeout_s=5, poll_interval=0.01)

    assert run(go([refuse(), refuse(), SUCCESS]))["status"]["completed"] is True
    with pytest.raises(ComfyUIConnectionError):
        run(go([refuse(), refuse(), refuse(), SUCCESS]))


def test_wait_auth_error_is_immediate():
    async def go():
        client, rec = make({"GET /history/p": jsonr({}, 401)})
        async with client:
            try:
                await client.wait("p", timeout_s=5, poll_interval=0.01)
            finally:
                assert len(rec.requests) == 1

    with pytest.raises(ComfyUIAuthError):
        run(go())


def test_wait_accepts_entry_without_status():
    async def go():
        client, _ = make({"GET /history/p": jsonr({"p": {"outputs": {}}})})
        async with client:
            return await client.wait("p", timeout_s=1, poll_interval=0.01)

    assert run(go()) == {"outputs": {}}


# ─── collect_outputs / fetch ─────────────────────────────────────
def test_collect_outputs_real_and_mixed_lists():
    client = ComfyUIClient("http://comfy:8188")
    try:
        entry = SUCCESS[SUCCESS_ID]
        assert client.collect_outputs(entry, ["2"]) == [
            {"node_id": "2", "filename": "xgen_probe_00001_.png", "subfolder": "", "type": "output", "kind": "image"},
        ]
        mixed = {"outputs": {
            "9": {
                "images": [{"filename": "a.png", "subfolder": "s", "type": "output"},
                           {"filename": "a.png", "subfolder": "s", "type": "output"}],
                "gifs": [{"filename": "clip.mp4", "subfolder": "", "type": "output", "format": "video/h264-mp4"}],
                "text": ["hello"],
                "animated": [True],
                "files": [{"filename": "mesh.glb"}, {"subfolder": "no-name"}],
            },
            "3": {"images": [{"filename": "preview.webp", "subfolder": "", "type": "temp"}]},
            "4": {"images": [{"filename": "ignored.png"}]},
        }}
        out = client.collect_outputs(mixed, ["3", "9"])
        assert out == [
            {"node_id": "3", "filename": "preview.webp", "subfolder": "", "type": "temp", "kind": "image"},
            {"node_id": "9", "filename": "a.png", "subfolder": "s", "type": "output", "kind": "image"},
            {"node_id": "9", "filename": "clip.mp4", "subfolder": "", "type": "output", "kind": "video"},
            {"node_id": "9", "filename": "mesh.glb", "subfolder": "", "type": "output", "kind": "file"},
        ]
        assert client.collect_outputs({"outputs": {}}, ["2"]) == []
        assert client.collect_outputs({}, ["2"]) == []
    finally:
        run(client.aclose())


def test_fetch_bytes_and_limits():
    def view(request):
        name = request.url.params["filename"]
        if name == "big.png":
            return httpx.Response(200, headers={"content-length": "999999"}, content=b"x" * 10)
        if name == "stream.png":
            async def body():
                for _ in range(5):
                    yield b"y" * 40
            return httpx.Response(200, content=body())
        if name == "gone.png":
            return httpx.Response(404)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async def go():
        client, rec = make({"GET /view": view})
        async with client:
            data = await client.fetch({"filename": "ok.png", "subfolder": "sub", "type": "temp"}, max_bytes=1000)
            errors = []
            for name in ("big.png", "stream.png", "gone.png"):
                try:
                    await client.fetch({"filename": name, "subfolder": "", "type": "output"}, max_bytes=100)
                except ComfyUIOutputError as exc:
                    errors.append(exc.code)
        return data, errors, rec

    data, errors, rec = run(go())
    assert data == PNG
    params = dict(rec.requests[0].url.params)
    assert params == {"filename": "ok.png", "subfolder": "sub", "type": "temp"}
    assert errors == ["output_too_large", "output_too_large", "no_output"]


def test_fetch_transport_errors():
    def slow(request):
        raise httpx.ReadTimeout("timeout", request=request)

    async def go():
        client, _ = make({"GET /view": slow})
        async with client:
            await client.fetch({"filename": "a.png"}, max_bytes=10)

    with pytest.raises(ComfyUITimeout):
        run(go())


# ─── interrupt / delete_queued ───────────────────────────────────
def test_interrupt_and_delete_bodies():
    async def go():
        client, rec = make({"POST /interrupt": lambda r: httpx.Response(200),
                            "POST /queue": lambda r: httpx.Response(200)})
        async with client:
            await client.interrupt("p1")
            await client.interrupt()
            await client.delete_queued(["p1"])
            await client.delete_queued([])
        return rec

    rec = run(go())
    assert [json.loads(r.content) for r in rec.calls("POST /interrupt")] == [{"prompt_id": "p1"}, {}]
    assert [json.loads(r.content) for r in rec.calls("POST /queue")] == [{"delete": ["p1"]}]


# ─── run_workflow ────────────────────────────────────────────────
class FakeServer:
    """submit → history → view 를 흉내 낸다. history 는 ``finish_after`` 번째에 끝난다."""

    def __init__(self, *, finish_after=1, outputs=None, prompt_delay=0.0):
        self.finish_after = finish_after
        self.polls = 0
        self.prompt_id = None
        self.prompt_delay = prompt_delay
        self.outputs = outputs if outputs is not None else {
            "2": {"images": [{"filename": f"xgen_{i:05d}_.png", "subfolder": "", "type": "output"} for i in range(3)]},
        }
        self.events = []

    async def prompt(self, request):
        body = json.loads(request.content)
        self.prompt_id = body.get("prompt_id") or "server-made"
        self.events.append(("prompt", self.prompt_id))
        if self.prompt_delay:
            await asyncio.sleep(self.prompt_delay)
        return httpx.Response(200, json={"prompt_id": self.prompt_id, "number": 1, "node_errors": {}})

    def history(self, request):
        self.polls += 1
        pid = request.url.path.rsplit("/", 1)[1]
        if self.finish_after is None or self.polls < self.finish_after:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={pid: {
            "outputs": self.outputs, "status": {"status_str": "success", "completed": True, "messages": []},
        }})

    def view(self, request):
        return httpx.Response(200, content=PNG + request.url.params["filename"].encode())

    async def queue(self, request):
        self.events.append(("delete", json.loads(request.content)["delete"]))
        return httpx.Response(200)

    async def interrupt(self, request):
        self.events.append(("interrupt", json.loads(request.content).get("prompt_id")))
        return httpx.Response(200)

    def routes(self):
        return {
            "POST /prompt": self.prompt,
            "GET /history/*": self.history,
            "GET /view": self.view,
            "POST /queue": self.queue,
            "POST /interrupt": self.interrupt,
        }


def test_run_workflow_success(monkeypatch):
    server = FakeServer(finish_after=2)

    async def go():
        client, _ = make(server.routes())
        async with client:
            return await run_workflow(client, GRAPH, output_node_ids=["2"], timeout_s=10, max_files=2)

    orig_wait = ComfyUIClient.wait

    async def fast_wait(self, prompt_id, *, timeout_s, poll_interval=1.0):
        return await orig_wait(self, prompt_id, timeout_s=timeout_s, poll_interval=0.01)

    monkeypatch.setattr(ComfyUIClient, "wait", fast_wait)
    result = run(go())
    assert result["prompt_id"] == server.prompt_id
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    assert [f["filename"] for f in result["files"]] == ["xgen_00000_.png", "xgen_00001_.png"]  # max_files
    assert result["files"][0] == {
        "node_id": "2", "filename": "xgen_00000_.png", "content_type": "image/png", "data": PNG + b"xgen_00000_.png",
    }
    assert not [e for e in server.events if e[0] in ("delete", "interrupt")]


def test_run_workflow_no_output():
    server = FakeServer(outputs={"2": {"images": []}})

    async def go():
        client, _ = make(server.routes())
        async with client:
            await run_workflow(client, GRAPH, output_node_ids=["2"], timeout_s=5)

    with pytest.raises(ComfyUIOutputError) as info:
        run(go())
    assert info.value.code == "no_output"


def test_run_workflow_cancel_sends_delete_then_interrupt():
    server = FakeServer(finish_after=None)

    async def go():
        client, _ = make(server.routes())
        async with client:
            task = asyncio.create_task(run_workflow(client, GRAPH, output_node_ids=["2"], timeout_s=60))
            while server.polls < 1:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(go())
    assert server.events == [
        ("prompt", server.prompt_id), ("delete", [server.prompt_id]), ("interrupt", server.prompt_id),
    ]


def test_run_workflow_cancel_during_submit_uses_the_prepared_id():
    server = FakeServer(prompt_delay=5)

    async def go():
        client, _ = make(server.routes())
        async with client:
            task = asyncio.create_task(run_workflow(client, GRAPH, output_node_ids=["2"], timeout_s=60))
            while server.prompt_id is None:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(go())
    pid = server.prompt_id
    assert len(pid) == 36 and pid == pid.lower()
    assert server.events == [("prompt", pid), ("delete", [pid]), ("interrupt", pid)]


def test_run_workflow_abandon_survives_a_second_cancel():
    server = FakeServer(finish_after=None)
    slow_queue = server.queue

    async def delayed_queue(request):
        await asyncio.sleep(0.15)
        return await slow_queue(request)

    async def go():
        routes = server.routes()
        routes["POST /queue"] = delayed_queue
        client, _ = make(routes)
        async with client:
            task = asyncio.create_task(run_workflow(client, GRAPH, output_node_ids=["2"], timeout_s=60))
            while server.polls < 1:
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()  # 정리 도중 한 번 더
            with pytest.raises(asyncio.CancelledError):
                await task
            # 백그라운드로 넘어간 정리가 마저 끝날 때까지 기다린다(클라이언트는 아직 열려 있다)
            for _ in range(100):
                if any(e[0] == "interrupt" for e in server.events):
                    break
                await asyncio.sleep(0.01)

    run(go())
    kinds = [e[0] for e in server.events]
    assert kinds == ["prompt", "delete", "interrupt"]


def test_run_workflow_timeout_interrupts(monkeypatch):
    server = FakeServer(finish_after=None)
    orig_wait = ComfyUIClient.wait

    async def fast_wait(self, prompt_id, *, timeout_s, poll_interval=1.0):
        return await orig_wait(self, prompt_id, timeout_s=timeout_s, poll_interval=0.02)

    monkeypatch.setattr(ComfyUIClient, "wait", fast_wait)

    async def go():
        client, _ = make(server.routes())
        async with client:
            await run_workflow(client, GRAPH, output_node_ids=["2"], timeout_s=0.15)

    with pytest.raises(ComfyUITimeout):
        run(go())
    assert [e[0] for e in server.events] == ["prompt", "delete", "interrupt"]


def test_run_workflow_prompt_and_execution_errors_do_not_interrupt():
    server = FakeServer()
    execution = _load("history_execution_error.json")

    async def go(routes):
        client, _ = make(routes)
        async with client:
            await run_workflow(client, GRAPH, output_node_ids=["2"], timeout_s=5)

    routes = server.routes()
    routes["POST /prompt"] = jsonr(_load("prompt_error_node_errors.json"), 400)
    with pytest.raises(ComfyUIPromptError):
        run(go(routes))

    routes = server.routes()
    routes["GET /history/*"] = lambda r: httpx.Response(
        200, json={r.url.path.rsplit("/", 1)[1]: next(iter(execution.values()))},
    )
    with pytest.raises(ComfyUIExecutionError):
        run(go(routes))
    assert not [e for e in server.events if e[0] in ("delete", "interrupt")]


def test_run_workflow_abandon_is_bounded(monkeypatch):
    monkeypatch.setattr(client_module, "ABANDON_TIMEOUT_S", 0.1)
    server = FakeServer(finish_after=None)

    async def hang(request):
        await asyncio.sleep(10)
        return httpx.Response(200)

    async def go():
        routes = server.routes()
        routes["POST /queue"] = hang
        client, _ = make(routes)
        async with client:
            task = asyncio.create_task(run_workflow(client, GRAPH, output_node_ids=["2"], timeout_s=60))
            while server.polls < 1:
                await asyncio.sleep(0.01)
            loop = asyncio.get_running_loop()
            started = loop.time()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return loop.time() - started

    # abandon_prompt 의 기본 인자는 정의 시점 값이라 모듈 상수 대신 직접 짧게 부른다
    orig = client_module.abandon_prompt

    async def short(client, prompt_id, *, timeout_s=0.1):
        return await orig(client, prompt_id, timeout_s=timeout_s)

    monkeypatch.setattr(client_module, "abandon_prompt", short)
    elapsed = run(go())
    assert elapsed < 2


def test_content_type_for():
    assert content_type_for("a.PNG") == "image/png"
    assert content_type_for("a.jpeg") == "image/jpeg"
    assert content_type_for("a.webp") == "image/webp"
    assert content_type_for("clip.mp4") == "video/mp4"
    assert content_type_for("noext") == "application/octet-stream"
