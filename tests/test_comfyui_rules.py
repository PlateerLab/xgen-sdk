"""ComfyUI 워크플로우 규칙 — 네트워크 없이 dict 위에서.

입력은 사용자가 준 실제 API export(Anima 워크플로우)와 실제 ComfyUI 0.35.0 의
``/object_info`` 응답을 줄인 사본이다(tests/fixtures/comfyui/).
"""
import copy
import json
import random
from pathlib import Path

import pytest

from xgen_sdk.comfyui import (
    ISSUE_CODES,
    MAPPING_VERSION,
    ArgumentError,
    WorkflowFormatError,
    apply_arguments,
    list_nodes,
    normalize_mapping,
    normalize_object_info,
    output_node_candidates,
    parse_workflow,
    tool_input_schema,
    validate_entry,
)

FIXTURES = Path(__file__).parent / "fixtures" / "comfyui"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def anima():
    return parse_workflow((FIXTURES / "workflow_anima_api.json").read_text(encoding="utf-8"))


@pytest.fixture
def raw_object_info():
    return _load("object_info_sample.json")


@pytest.fixture
def object_info(raw_object_info):
    return normalize_object_info(raw_object_info)


def _param(name, node_id, input_name, **extra):
    base = {"name": name, "type": "string", "targets": [{"node_id": node_id, "input": input_name}]}
    base.update(extra)
    return base


def anima_mapping():
    return {
        "version": 1,
        "params": [
            _param("prompt", "90:77", "text", description="그릴 장면을 영어로 적습니다.", required=True, multiline=True),
            _param("negative_prompt", "90:75", "text", multiline=True),
            _param("seed", "90:76", "seed", type="integer", randomize=True, minimum=0),
            _param("steps", "90:79", "value", type="integer", default=30, minimum=1, maximum=100),
            _param("cfg", "90:86", "value", type="number", default=4, minimum=1, maximum=10),
            _param("aspect_ratio", "91", "aspect_ratio", default="1:1 (Square)",
                   choices=["1:1 (Square)", "16:9 (Landscape)", "9:16 (Portrait)"]),
            _param("turbo", "90:89", "value", type="boolean"),
        ],
        "outputs": ["46"],
    }


# ─── parse_workflow ──────────────────────────────────────────────
def test_parse_user_example_api_export(anima):
    assert len(anima) == 16
    assert anima["91"]["class_type"] == "ResolutionSelector"
    assert anima["90:73"]["inputs"]["samples"] == ["90:76", 0]


def test_parse_returns_deep_copy():
    src = {"1": {"class_type": "EmptyImage", "inputs": {"width": 8, "meta": {"a": [1]}}}}
    out = parse_workflow(src)
    out["1"]["inputs"]["meta"]["a"].append(2)
    assert src["1"]["inputs"]["meta"]["a"] == [1]


@pytest.mark.parametrize(
    "raw, code",
    [
        ("{not json", "invalid_json"),
        ("[1, 2]", "invalid_json"),
        (json.dumps({"nodes": [], "links": [], "version": 0.4}), "ui_format"),
        ({"last_node_id": 3, "links": []}, "ui_format"),
        ({}, "empty"),
        ("{}", "empty"),
    ],
)
def test_parse_rejections(raw, code):
    with pytest.raises(WorkflowFormatError) as info:
        parse_workflow(raw)
    assert info.value.code == code
    assert "—" not in info.value.message


def test_parse_ui_format_message_is_the_editor_sentence():
    with pytest.raises(WorkflowFormatError) as info:
        parse_workflow({"nodes": [{"id": 1}], "links": []})
    assert info.value.message == "API 형식으로 내보낸 JSON을 넣어 주세요."


@pytest.mark.parametrize(
    "graph, node_id",
    [
        ({"1": {"class_type": "A", "inputs": {}}, "2": "oops"}, "2"),
        ({"7": {"inputs": {}}}, "7"),
        ({"7": {"class_type": "", "inputs": {}}}, "7"),
        ({"9": {"class_type": "A"}}, "9"),
        ({"9": {"class_type": "A", "inputs": []}}, "9"),
        ({"9": {"class_type": "A", "inputs": {}, "_meta": "x"}}, "9"),
    ],
)
def test_parse_invalid_node_reports_node_id(graph, node_id):
    with pytest.raises(WorkflowFormatError) as info:
        parse_workflow(graph)
    assert info.value.code == "invalid_node"
    assert info.value.node_id == node_id


def test_a_node_literally_named_nodes_is_not_ui_format():
    graph = parse_workflow({"nodes": {"class_type": "SaveImage", "inputs": {}}})
    assert list(graph) == ["nodes"]


# ─── list_nodes ──────────────────────────────────────────────────
def test_list_nodes_natural_order_and_input_shapes(anima):
    nodes = list_nodes(anima)
    ids = [n["node_id"] for n in nodes]
    assert ids == [
        "46", "90:71", "90:72", "90:73", "90:74", "90:75", "90:76", "90:77",
        "90:78", "90:79", "90:84", "90:85", "90:86", "90:87", "90:89", "91",
    ]
    by_id = {n["node_id"]: n for n in nodes}
    sampler = by_id["90:76"]
    assert sampler["class_type"] == "KSampler" and sampler["title"] == "KSampler"
    inputs = {i["name"]: i for i in sampler["inputs"]}
    assert [i["name"] for i in sampler["inputs"]][:3] == ["seed", "steps", "cfg"]
    assert inputs["seed"] == {
        "name": "seed", "value": 387936172144698, "is_link": False, "link": None, "inferred_type": "integer",
    }
    assert inputs["steps"]["is_link"] is True
    assert inputs["steps"]["link"] == ["90:85", 0]
    assert inputs["steps"]["inferred_type"] == "unknown"
    assert inputs["sampler_name"]["inferred_type"] == "string"
    assert inputs["denoise"]["inferred_type"] == "integer"
    assert {i["name"]: i["inferred_type"] for i in by_id["90:89"]["inputs"]} == {"value": "boolean"}
    assert {i["name"]: i["inferred_type"] for i in by_id["90:86"]["inputs"]} == {"value": "integer"}
    assert by_id["91"]["title"] == "해상도 선택기"


def test_list_nodes_title_falls_back_to_class_type_and_number_types():
    graph = {
        "10": {"class_type": "X", "inputs": {"f": 0.5, "n": None, "l": [1, 2]}},
        "9": {"class_type": "Y", "inputs": {}, "_meta": {"title": ""}},
        "90:100": {"class_type": "Z", "inputs": {}},
        "90:99": {"class_type": "Z", "inputs": {}},
    }
    nodes = list_nodes(graph)
    assert [n["node_id"] for n in nodes] == ["9", "10", "90:99", "90:100"]
    assert nodes[0]["title"] == "Y"
    types = {i["name"]: (i["inferred_type"], i["is_link"]) for i in nodes[1]["inputs"]}
    assert types == {"f": ("number", False), "n": ("unknown", False), "l": ("unknown", False)}


# ─── normalize_object_info ───────────────────────────────────────
def test_normalize_object_info_real_shapes(object_info):
    ks = object_info["KSampler"]
    assert ks["seed"] == {
        "kind": "INT", "default": 0, "min": 0, "max": 18446744073709551615, "step": None,
        "choices": None, "multiline": False, "tooltip": "The random seed used for creating the noise.",
    }
    assert ks["cfg"]["kind"] == "FLOAT" and ks["cfg"]["step"] == 0.1 and ks["cfg"]["max"] == 100.0
    assert ks["sampler_name"]["kind"] == "COMBO" and "euler" in ks["sampler_name"]["choices"]
    assert ks["model"]["kind"] == "LINK" and ks["model"]["tooltip"]
    assert object_info["CLIPTextEncode"]["text"]["kind"] == "STRING"
    assert object_info["CLIPTextEncode"]["text"]["multiline"] is True
    assert object_info["EmptyLatentImage"]["width"]["step"] == 8
    # 모델이 없는 로더는 빈 콤보 — 그대로 둔다
    assert object_info["UNETLoader"]["unet_name"] == {
        "kind": "COMBO", "default": None, "min": None, "max": None, "step": None,
        "choices": [], "multiline": False, "tooltip": None,
    }
    assert object_info["UNETLoader"]["weight_dtype"]["choices"][0] == "default"
    assert object_info["LoadImage"]["image"]["choices"] == ["example.png"]
    save = object_info["SaveImage"]
    assert list(save) == ["images", "filename_prefix"]  # hidden 제외
    assert save["images"]["kind"] == "LINK" and save["filename_prefix"]["default"] == "ComfyUI"
    assert object_info["PrimitiveInt"]["value"]["kind"] == "INT"
    assert object_info["PrimitiveBoolean"]["value"]["kind"] == "BOOLEAN"
    switch = object_info["ComfySwitchNode"]
    assert switch["switch"]["kind"] == "BOOLEAN"
    assert switch["on_false"]["kind"] == "LINK" and switch["on_true"]["kind"] == "LINK"  # optional 포함
    assert object_info["ImageCropToMask"]["background"]["kind"] == "OTHER"
    assert object_info["ImageCropToMask"]["background"]["default"] == "#000000"
    assert object_info["ResizeImageMaskNode"]["resize_type"]["kind"] == "COMBO"
    assert "scale dimensions" in object_info["ResizeImageMaskNode"]["resize_type"]["choices"]
    assert object_info["PreviewAny"]["source"]["kind"] == "LINK"


def test_normalize_object_info_v3_combo_and_odd_specs():
    raw = {
        "N": {
            "input": {
                "required": {
                    "a": ["COMBO", {"options": ["x", "y"], "default": "y"}],
                    "b": ["COMBO", {"image_upload": True}],
                    "c": "garbage",
                    "d": [],
                    "e": [{"weird": 1}],
                },
                "optional": {"a": ["INT", {}]},
            },
            "output_node": False,
        },
        "Empty": {"output_node": True},
        "bad": "x",
    }
    out = normalize_object_info(raw)
    assert out["N"]["a"]["choices"] == ["x", "y"] and out["N"]["a"]["default"] == "y"
    assert out["N"]["b"]["kind"] == "COMBO" and out["N"]["b"]["choices"] == []
    assert out["N"]["c"]["kind"] == "OTHER" and out["N"]["d"]["kind"] == "OTHER" and out["N"]["e"]["kind"] == "OTHER"
    assert out["Empty"] == {}
    assert "bad" not in out
    assert normalize_object_info(None) == {}


# ─── output_node_candidates ──────────────────────────────────────
def test_output_candidates_user_example(anima, raw_object_info, object_info):
    assert output_node_candidates(anima) == ["46"]
    assert output_node_candidates(anima, raw_object_info) == ["46"]
    assert output_node_candidates(anima, object_info) == ["46"]  # 정규화본은 이름 추정


def test_output_candidates_object_info_wins_over_names():
    graph = {
        "1": {"class_type": "MyCustomSaver", "inputs": {}},
        "2": {"class_type": "SaveImage", "inputs": {}},
        "3": {"class_type": "PreviewImage", "inputs": {}},
        "4": {"class_type": "SaveAnimatedWEBP", "inputs": {}},
        "5": {"class_type": "KSampler", "inputs": {}},
        "6": {"class_type": "PreviewAny", "inputs": {}},
    }
    assert output_node_candidates(graph) == ["2", "3", "4", "6"]
    info = {"MyCustomSaver": {"output_node": True}, "SaveImage": {"output_node": False}}
    assert output_node_candidates(graph, info) == ["1", "3", "4", "6"]


# ─── normalize_mapping ───────────────────────────────────────────
def test_normalize_mapping_fills_defaults():
    assert normalize_mapping(None) == {"version": MAPPING_VERSION, "params": [], "outputs": []}
    out = normalize_mapping({"params": [{"name": "prompt", "targets": [{"node_id": 46, "input": "text"}], "extra": 1}],
                             "outputs": [46, "", None]})
    assert out == {
        "version": 1,
        "params": [{
            "name": "prompt", "description": "", "type": "string", "required": False, "default": None,
            "multiline": False, "minimum": None, "maximum": None, "choices": None, "randomize": False,
            "targets": [{"node_id": "46", "input": "text"}],
        }],
        "outputs": ["46"],
    }
    assert normalize_mapping(json.dumps({"outputs": ["1"]}))["outputs"] == ["1"]
    assert normalize_mapping("not json") == {"version": 1, "params": [], "outputs": []}
    weird = normalize_mapping({"params": ["x"], "outputs": "46"})
    assert weird["params"][0]["name"] == "" and weird["outputs"] == []


# ─── validate_entry ──────────────────────────────────────────────
def test_user_example_mapping_is_valid(anima, raw_object_info, object_info):
    mapping = anima_mapping()
    assert validate_entry(anima, mapping) == []
    assert validate_entry(anima, mapping, object_info) == []
    assert validate_entry(anima, mapping, raw_object_info) == []


def _codes(issues):
    return [i["code"] for i in issues]


def _only(anima, mapping, object_info=None):
    issues = validate_entry(anima, mapping, object_info)
    for issue in issues:
        assert set(issue) == {"path", "code", "message"}
        assert issue["message"] and "—" not in issue["message"]
    return issues


def test_each_issue_code(anima, object_info):
    seen = set()

    def check(mapping, code, path=None, info=None):
        issues = _only(anima, mapping, info)
        assert code in _codes(issues), (code, issues)
        if path:
            assert path in [i["path"] for i in issues if i["code"] == code], issues
        seen.add(code)

    base = anima_mapping

    m = base(); m["params"][0]["name"] = "Prompt"
    check(m, "name_invalid", "params[0].name")
    m = base(); m["params"][0]["name"] = "a" * 41
    check(m, "name_invalid")
    m = base(); m["params"][1]["name"] = "prompt"
    check(m, "name_duplicate", "params[1].name")
    m = base(); m["params"] = [_param(f"p{i}", "90:77", "text") for i in range(31)]
    check(m, "too_many_params", "params")
    m = base(); m["params"][0]["targets"] = []
    check(m, "targets_empty", "params[0].targets")
    m = base(); m["params"][0]["targets"] = [{"node_id": "999", "input": "text"}]
    check(m, "target_missing_node", "params[0].targets[0].node_id")
    m = base(); m["params"][0]["targets"] = [{"node_id": "90:77", "input": "nope"}]
    check(m, "target_missing_input", "params[0].targets[0].input")
    m = base(); m["params"][3]["targets"] = [{"node_id": "90:76", "input": "steps"}]
    check(m, "target_is_link", "params[3].targets[0].input")
    m = base(); m["params"][1]["targets"] = [{"node_id": "90:77", "input": "text"}]
    check(m, "target_duplicate", "params[1].targets[0]")
    m = base(); m["params"][2]["type"] = "string"; m["params"][2]["randomize"] = False
    check(m, "type_mismatch", "params[2].targets[0]", info=object_info)
    m = base(); m["params"][0]["type"] = "int"
    check(m, "type_mismatch", "params[0].type")
    m = base(); m["params"][3]["default"] = 500
    check(m, "default_out_of_range", "params[3].default")
    m = base(); m["params"][3]["minimum"] = 50; m["params"][3]["maximum"] = 10; m["params"][3]["default"] = None
    check(m, "default_out_of_range", "params[3].minimum")
    m = base(); m["params"][5]["default"] = "4:3"
    check(m, "default_not_in_choices", "params[5].default")
    m = base(); m["params"][3]["default"] = "30"
    check(m, "default_type", "params[3].default")
    m = base(); m["params"][6]["default"] = 1
    check(m, "default_type", "params[6].default")
    m = base(); m["params"][5]["choices"] = []
    check(m, "choices_empty", "params[5].choices")
    m = base(); m["outputs"] = []
    check(m, "outputs_empty", "outputs")
    m = base(); m["outputs"] = ["46", "77"]
    check(m, "output_not_found", "outputs[1]")
    m = base(); m["params"][3]["randomize"] = True; m["params"][3]["type"] = "number"
    check(m, "randomize_not_integer", "params[3].randomize")
    m = base(); m["params"][0]["description"] = "가" * 501
    check(m, "description_too_long", "params[0].description")

    assert seen == set(ISSUE_CODES)


def test_type_checks_follow_object_info_kinds(anima, object_info):
    def issues_for(ptype, node_id, input_name, info=object_info):
        m = {"params": [_param("x", node_id, input_name, type=ptype)], "outputs": ["46"]}
        return _codes(validate_entry(anima, m, info))

    assert issues_for("number", "90:86", "value") == []           # FLOAT ← number
    assert issues_for("integer", "90:86", "value") == []          # FLOAT ← integer
    assert issues_for("number", "90:79", "value") == ["type_mismatch"]  # INT ← number
    assert issues_for("string", "90:76", "sampler_name") == []     # COMBO ← string
    assert issues_for("boolean", "90:76", "sampler_name") == ["type_mismatch"]
    assert issues_for("boolean", "90:89", "value") == []
    assert issues_for("string", "90:89", "value") == ["type_mismatch"]
    # 규격이 없는 커스텀 노드: 값 모양으로 추정 (megapixels: 1 → 정수/숫자 허용)
    assert issues_for("number", "91", "megapixels") == []
    assert issues_for("integer", "91", "megapixels") == []
    assert issues_for("string", "91", "megapixels") == ["type_mismatch"]
    assert issues_for("integer", "91", "aspect_ratio") == ["type_mismatch"]
    # object_info 없이도 명백한 불일치는 잡는다
    assert issues_for("integer", "90:77", "text", info=None) == ["type_mismatch"]


def test_choice_item_types_and_boolean_choices(anima):
    m = {"params": [_param("steps", "90:79", "value", type="integer", choices=[10, "twenty"])], "outputs": ["46"]}
    assert [(i["path"], i["code"]) for i in validate_entry(anima, m)] == [("params[0].choices[1]", "type_mismatch")]
    m = {"params": [_param("turbo", "90:89", "value", type="boolean", choices=[True])], "outputs": ["46"]}
    assert [(i["path"], i["code"]) for i in validate_entry(anima, m)] == [("params[0].choices", "type_mismatch")]


# ─── tool_input_schema ───────────────────────────────────────────
def test_tool_input_schema_shape():
    schema = tool_input_schema(anima_mapping())
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["prompt"]
    props = schema["properties"]
    assert list(props) == ["prompt", "negative_prompt", "seed", "steps", "cfg", "aspect_ratio", "turbo"]
    assert props["prompt"] == {"type": "string", "description": "그릴 장면을 영어로 적습니다."}
    assert props["negative_prompt"] == {"type": "string"}
    assert props["seed"] == {"type": "integer", "description": "Leave empty to use a random value.", "minimum": 0}
    assert props["steps"] == {"type": "integer", "description": "Default: 30.", "minimum": 1, "maximum": 100}
    assert props["aspect_ratio"]["enum"] == ["1:1 (Square)", "16:9 (Landscape)", "9:16 (Portrait)"]
    assert props["aspect_ratio"]["description"] == 'Default: "1:1 (Square)".'
    assert props["turbo"] == {"type": "boolean"}
    json.dumps(schema)


def test_tool_input_schema_empty_mapping():
    assert tool_input_schema(None) == {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }


# ─── apply_arguments ─────────────────────────────────────────────
def test_apply_arguments_sets_values_on_a_copy(anima):
    before = copy.deepcopy(anima)
    out = apply_arguments(anima, anima_mapping(), {"prompt": "a red fox", "steps": "25", "turbo": "true"},
                          rng=random.Random(7))
    assert anima == before
    assert out["90:77"]["inputs"]["text"] == "a red fox"
    assert out["90:79"]["inputs"]["value"] == 25
    assert out["90:89"]["inputs"]["value"] is True
    assert out["90:86"]["inputs"]["value"] == 4             # 기본값
    assert out["91"]["inputs"]["aspect_ratio"] == "1:1 (Square)"
    # 인자도 기본값도 없으면 워크플로우 값 그대로
    assert out["90:75"]["inputs"]["text"] == anima["90:75"]["inputs"]["text"]
    # randomize: 비우면 무작위, 범위 안
    seed = out["90:76"]["inputs"]["seed"]
    assert isinstance(seed, int) and 0 <= seed <= 2 ** 53 - 1
    assert seed == random.Random(7).randint(0, 2 ** 53 - 1)
    # 링크는 건드리지 않는다
    assert out["90:76"]["inputs"]["steps"] == ["90:85", 0]


def test_apply_arguments_given_seed_and_randomize_bounds(anima):
    out = apply_arguments(anima, anima_mapping(), {"prompt": "x", "seed": 42})
    assert out["90:76"]["inputs"]["seed"] == 42
    m = {"params": [_param("seed", "90:76", "seed", type="integer", randomize=True, minimum=5, maximum=7)],
         "outputs": ["46"]}
    values = {apply_arguments(anima, m, {"seed": None}, rng=random.Random(i))["90:76"]["inputs"]["seed"] for i in range(40)}
    assert values <= {5, 6, 7} and len(values) > 1
    m["params"][0]["maximum"] = 2 ** 60
    big = apply_arguments(anima, m, {}, rng=random.Random(1))["90:76"]["inputs"]["seed"]
    assert big <= 2 ** 53 - 1


def test_apply_arguments_multiple_targets(anima):
    m = {"params": [{"name": "text", "type": "string",
                     "targets": [{"node_id": "90:77", "input": "text"}, {"node_id": "90:75", "input": "text"}]}],
         "outputs": ["46"]}
    out = apply_arguments(anima, m, {"text": "same"})
    assert out["90:77"]["inputs"]["text"] == "same" and out["90:75"]["inputs"]["text"] == "same"


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"prompt": "x", "style": "y"}, "Unknown argument(s): style"),
        ({}, "Missing required argument: prompt"),
        ({"prompt": None}, "Missing required argument: prompt"),
        ({"prompt": "x", "steps": 0}, "between 1 and 100"),
        ({"prompt": "x", "steps": 2.5}, "must be an integer"),
        ({"prompt": "x", "steps": True}, "must be an integer"),
        ({"prompt": "x", "cfg": "hot"}, "must be a number"),
        ({"prompt": "x", "cfg": float("nan")}, "must be a number"),
        ({"prompt": "x", "aspect_ratio": "4:3"}, "must be one of"),
        ({"prompt": "x", "turbo": "maybe"}, "must be a boolean"),
        ({"prompt": ["x"]}, "must be a string"),
    ],
)
def test_apply_arguments_errors(anima, arguments, fragment):
    with pytest.raises(ArgumentError) as info:
        apply_arguments(anima, anima_mapping(), arguments)
    assert fragment in info.value.message


def test_apply_arguments_coercions(anima):
    out = apply_arguments(anima, anima_mapping(), {"prompt": 5, "steps": 7.0, "cfg": "4.5", "turbo": 0, "seed": ""})
    assert out["90:77"]["inputs"]["text"] == "5"
    assert out["90:79"]["inputs"]["value"] == 7 and isinstance(out["90:79"]["inputs"]["value"], int)
    assert out["90:86"]["inputs"]["value"] == 4.5
    assert out["90:89"]["inputs"]["value"] is False
    assert isinstance(out["90:76"]["inputs"]["seed"], int)
    # 문자열 형은 빈 문자열도 값이다
    assert apply_arguments(anima, anima_mapping(), {"prompt": ""})["90:77"]["inputs"]["text"] == ""


def test_apply_arguments_rejects_non_object_and_broken_targets(anima):
    with pytest.raises(ArgumentError):
        apply_arguments(anima, anima_mapping(), ["prompt"])
    m = {"params": [_param("steps", "90:76", "steps", type="integer")], "outputs": ["46"]}
    with pytest.raises(ArgumentError) as info:
        apply_arguments(anima, m, {"steps": 3})
    assert "tool configuration" in info.value.message
