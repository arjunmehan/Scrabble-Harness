import json

from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError
import pytest
from pydantic import ValidationError

from scrabble_harness.harness import (
    CompareAction, PresentationVersionError, SelectAction, ToolDefinition,
    ToolSchemaError, parse_tool_arguments, tool_definitions,
)


def test_packaged_function_schemas_are_valid_separate_strict_objects():
    definitions = tool_definitions()
    assert tuple(definition.name for definition in definitions) == ("select_move", "compare_moves")
    for definition in definitions:
        transport = definition.as_function_tool()
        parameters = transport["parameters"]
        Draft202012Validator.check_schema(parameters)
        assert transport["strict"] is True and transport["type"] == "function"
        assert parameters["type"] == "object" and parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])
        assert "schema_version" not in parameters["properties"]
        assert "kind" not in parameters["properties"]
        assert "uniqueItems" not in json.dumps(parameters)
        assert ToolDefinition.model_validate_json(definition.model_dump_json()) == definition


@pytest.mark.parametrize("name,arguments,action_type", [
    ("select_move", {"candidate_handle": "c01", "evidence_ids": [], "brief_note": ""}, SelectAction),
    ("select_move", {"candidate_handle": "c01", "evidence_ids": ["e01"], "brief_note": "Balanced leave"}, SelectAction),
    ("compare_moves", {"candidate_handles": ["c01", "c02"], "tier": "light"}, CompareAction),
    ("compare_moves", {"candidate_handles": ["c01", "c02", "c03", "c04"], "tier": "standard"}, CompareAction),
])
def test_schema_and_host_parser_accept_the_same_arguments(name, arguments, action_type):
    definition = next(item for item in tool_definitions() if item.name == name)
    Draft202012Validator(definition.as_function_tool()["parameters"]).validate(arguments)
    assert isinstance(parse_tool_arguments(name, json.dumps(arguments)), action_type)


@pytest.mark.parametrize("name,arguments", [
    ("select_move", {"candidate_handle": "c01"}),
    ("select_move", {"candidate_handle": "c01", "evidence_ids": [], "brief_note": "", "kind": "select"}),
    ("select_move", {"candidate_handle": "c01", "evidence_ids": None, "brief_note": ""}),
    ("select_move", {"candidate_handle": "cand_v1_" + "a" * 64, "evidence_ids": [], "brief_note": ""}),
    ("select_move", {"candidate_handle": "c01", "evidence_ids": [], "brief_note": "x" * 501}),
    ("compare_moves", {"candidate_handles": ["c01"], "tier": "light"}),
    ("compare_moves", {"candidate_handles": ["c01", "c02", "c03", "c04", "c05"], "tier": "light"}),
    ("compare_moves", {"candidate_handles": ["c01", "c02"], "tier": "unlimited"}),
    ("compare_moves", {"candidate_handles": ["c01", True], "tier": "light"}),
    ("compare_moves", {"candidate_handles": ["c01", "c02"], "tier": "light", "native_command": "sim"}),
])
def test_bad_arguments_fail_both_schema_and_host_validation(name, arguments):
    definition = next(item for item in tool_definitions() if item.name == name)
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(definition.as_function_tool()["parameters"]).validate(arguments)
    with pytest.raises(ToolSchemaError):
        parse_tool_arguments(name, json.dumps(arguments))


@pytest.mark.parametrize("name,arguments", [
    ("select_move", '{"candidate_handle":"c01","evidence_ids":["e01","e01"],"brief_note":""}'),
    ("compare_moves", '{"candidate_handles":["c01","c01"],"tier":"light"}'),
    ("select_move", '{"candidate_handle":"c01","candidate_handle":"c02","evidence_ids":[],"brief_note":""}'),
    ("select_move", '[]'), ("select_move", '{broken'),
    ("select_move", '{"candidate_handle":NaN,"evidence_ids":[],"brief_note":""}'),
    ("select_move", '{"candidate_handle":1e999,"evidence_ids":[],"brief_note":""}'),
])
def test_semantic_or_ambiguous_json_is_rejected_without_guessing(name, arguments):
    with pytest.raises(ToolSchemaError):
        parse_tool_arguments(name, arguments)


def test_disabled_comparison_and_unknown_tools_fail():
    for name in ("compare_moves", "commit_game", "show_more_candidates"):
        with pytest.raises(ToolSchemaError, match="not offered"):
            parse_tool_arguments(name, '{}', allowed_tools=("select_move",))


def test_exported_schemas_are_detached_and_versions_fail_closed():
    first = tool_definitions()[0].as_function_tool()
    first["parameters"]["additionalProperties"] = True
    assert tool_definitions()[0].as_function_tool()["parameters"]["additionalProperties"] is False
    with pytest.raises(PresentationVersionError):
        tool_definitions(version="../other")
    with pytest.raises(ToolSchemaError):
        tool_definitions(("select_move", "select_move"))


def test_argument_ceiling_counts_utf8_bytes():
    arguments = json.dumps({"candidate_handle": "c01", "evidence_ids": [], "brief_note": "🙂" * 300}, ensure_ascii=False)
    assert len(arguments) < 1000
    with pytest.raises(ToolSchemaError):
        parse_tool_arguments("select_move", arguments, max_argument_bytes=1000)


@pytest.mark.parametrize("changes", [
    {"strict": False}, {"strict": 1},
    {"parameters_json": '{"type":"object","properties":{},"required":[{}],"additionalProperties":false}'},
    {"parameters_json": '{"type":"object","properties":{"x":{"type":"string"}},"required":[],"additionalProperties":false}'},
])
def test_tool_records_reject_non_strict_schemas(changes):
    fields = tool_definitions()[0].model_dump(mode="python") | changes
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate(fields)
