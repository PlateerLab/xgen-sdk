"""xgen_sdk.comfyui — ComfyUI 워크플로우 규칙과 클라이언트의 단일 정본 (2.4.0+).

관리 화면(xgen-core)과 에이전트 도구(xgen-workflow)가 같은 규칙을 본다.
이 패키지는 DB·config·암호화를 모른다 — 워크플로우 JSON, 매핑, 서버 주소와
헤더만 받는다.

    workflow.py     API 형식 워크플로우 읽기, 노드 목록, 출력 노드 후보
    object_info.py  ``/object_info`` 입력 규격 정규화
    mapping.py      노출 파라미터 매핑: 정규화, 검증, 도구 스키마, 인자 적용
    client.py       httpx async 클라이언트와 ``run_workflow``
    errors.py       오류 계층

Usage:
    from xgen_sdk.comfyui import (
        parse_workflow, validate_entry, tool_input_schema, apply_arguments,
        ComfyUIClient, run_workflow,
    )

    graph = parse_workflow(workflow_json)
    issues = validate_entry(graph, mapping, object_info)
    prompt = apply_arguments(graph, mapping, {"prompt": "a cat"})
    async with ComfyUIClient("http://comfy:8188", headers={"Authorization": "Bearer ..."}) as client:
        result = await run_workflow(client, prompt, output_node_ids=mapping["outputs"], timeout_s=300)
"""
from xgen_sdk.comfyui.client import (
    ABANDON_TIMEOUT_S,
    POLL_FAILURE_LIMIT,
    ComfyUIClient,
    abandon_prompt,
    content_type_for,
    run_workflow,
    summarize_node_errors,
)
from xgen_sdk.comfyui.errors import (
    ArgumentError,
    ComfyUIAuthError,
    ComfyUIConnectionError,
    ComfyUIError,
    ComfyUIExecutionError,
    ComfyUIOutputError,
    ComfyUIPromptError,
    ComfyUITimeout,
    WorkflowFormatError,
)
from xgen_sdk.comfyui.mapping import (
    ISSUE_CODES,
    MAPPING_VERSION,
    MAX_DESCRIPTION_LENGTH,
    MAX_PARAMS,
    PARAM_NAME_PATTERN,
    PARAM_TYPES,
    apply_arguments,
    normalize_mapping,
    tool_input_schema,
    validate_entry,
)
from xgen_sdk.comfyui.object_info import INPUT_KINDS, normalize_object_info
from xgen_sdk.comfyui.workflow import list_nodes, output_node_candidates, parse_workflow

__all__ = [
    # 규칙
    "MAPPING_VERSION",
    "PARAM_TYPES",
    "PARAM_NAME_PATTERN",
    "MAX_PARAMS",
    "MAX_DESCRIPTION_LENGTH",
    "ISSUE_CODES",
    "INPUT_KINDS",
    "parse_workflow",
    "list_nodes",
    "normalize_object_info",
    "output_node_candidates",
    "normalize_mapping",
    "validate_entry",
    "tool_input_schema",
    "apply_arguments",
    # 클라이언트
    "ComfyUIClient",
    "run_workflow",
    "abandon_prompt",
    "content_type_for",
    "summarize_node_errors",
    "ABANDON_TIMEOUT_S",
    "POLL_FAILURE_LIMIT",
    # 오류
    "ArgumentError",
    "WorkflowFormatError",
    "ComfyUIError",
    "ComfyUIConnectionError",
    "ComfyUIAuthError",
    "ComfyUIPromptError",
    "ComfyUITimeout",
    "ComfyUIExecutionError",
    "ComfyUIOutputError",
]
