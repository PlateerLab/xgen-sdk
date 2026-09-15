"""실제 ComfyUI 서버 통합 시험 — ``COMFYUI_TEST_URL`` 이 있을 때만 돈다.

모델 없이 도는 ``EmptyImage → SaveImage`` 만 실행한다(공용 서버에 무거운 작업을
올리지 않는다).

    COMFYUI_TEST_URL=http://127.0.0.1:18188 pytest tests/test_comfyui_integration.py
"""
import asyncio
import os
import struct
import zlib

import pytest

from xgen_sdk.comfyui import (
    ComfyUIClient,
    ComfyUIPromptError,
    apply_arguments,
    list_nodes,
    normalize_object_info,
    output_node_candidates,
    parse_workflow,
    tool_input_schema,
    validate_entry,
    run_workflow,
)

COMFYUI_URL = os.environ.get("COMFYUI_TEST_URL", "").strip()

pytestmark = pytest.mark.skipif(not COMFYUI_URL, reason="COMFYUI_TEST_URL is not set")

WORKFLOW = {
    "1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 48, "batch_size": 1, "color": 0},
          "_meta": {"title": "Empty Image"}},
    "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "xgen_sdk_it", "images": ["1", 0]},
          "_meta": {"title": "Save Image"}},
}

MAPPING = {
    "version": 1,
    "params": [
        {"name": "width", "type": "integer", "default": 32, "minimum": 8, "maximum": 256,
         "targets": [{"node_id": "1", "input": "width"}]},
        {"name": "height", "type": "integer", "default": 24, "minimum": 8, "maximum": 256,
         "targets": [{"node_id": "1", "input": "height"}]},
        {"name": "color", "type": "integer", "minimum": 0, "maximum": 16777215,
         "targets": [{"node_id": "1", "input": "color"}]},
    ],
    "outputs": ["2"],
}


def _png_size_and_pixel(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    pos, idat = 8, b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        if kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    # 첫 줄 필터 바이트(0 가정) 다음 RGB
    return width, height, tuple(raw[1:4])


def test_real_server_end_to_end():
    async def go():
        async with ComfyUIClient(COMFYUI_URL, timeout=30) as client:
            stats = await client.system_stats()
            assert isinstance(stats["system"]["comfyui_version"], str)

            raw_info = await client.object_info(["EmptyImage", "SaveImage", "NoSuchNodeXYZ"])
            assert sorted(raw_info) == ["EmptyImage", "SaveImage"]
            info = normalize_object_info(raw_info)
            assert info["EmptyImage"]["width"]["kind"] == "INT"
            assert info["SaveImage"]["images"]["kind"] == "LINK"

            graph = parse_workflow(WORKFLOW)
            assert [n["node_id"] for n in list_nodes(graph)] == ["1", "2"]
            assert output_node_candidates(graph, raw_info) == ["2"]
            assert validate_entry(graph, MAPPING, raw_info) == []
            assert set(tool_input_schema(MAPPING)["properties"]) == {"width", "height", "color"}

            prompt = apply_arguments(graph, MAPPING, {"color": 0x00FF00})
            result = await run_workflow(client, prompt, output_node_ids=MAPPING["outputs"], timeout_s=120)
            assert len(result["files"]) == 1
            file = result["files"][0]
            assert file["node_id"] == "2" and file["content_type"] == "image/png"
            assert file["filename"].startswith("xgen_sdk_it")
            width, height, rgb = _png_size_and_pixel(file["data"])
            assert (width, height) == (32, 24)
            assert rgb == (0, 255, 0)
            assert result["duration_ms"] >= 0

            # 검증 실패와 없는 노드는 실제 서버에서도 요약된다
            bad = apply_arguments(graph, MAPPING, {})
            bad["1"]["inputs"]["width"] = "abc"
            with pytest.raises(ComfyUIPromptError) as info_err:
                await client.submit(bad)
            assert any(e["input"] == "width" for e in info_err.value.node_errors)
            with pytest.raises(ComfyUIPromptError) as missing:
                await client.submit({"1": {"class_type": "NoSuchNodeXYZ", "inputs": {}}})
            assert missing.value.error_type == "missing_node_type"

            # 돌고 있지 않은 id 로의 정리 요청은 조용히 지나간다(다른 작업을 멈추지 않는다)
            await client.delete_queued([result["prompt_id"]])
            await client.interrupt(result["prompt_id"])

    asyncio.run(go())
