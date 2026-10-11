"""Versioned, SDK-independent function definitions and strict argument parsing.

Schemas intentionally stay stable across positions. Visibility, evidence
ownership, tier availability, and admission are checked by trusted host code.
No action is executed here; malformed arguments remain in the original reply.
"""

from functools import lru_cache
from importlib.resources import files
import json
import math
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from ..domain._base import DomainModel
from .contracts import CompareAction, SelectAction
from .errors import PresentationVersionError, ToolSchemaError

TOOL_SCHEMA_VERSION = "scrabble-tools-v1"
ToolName = Literal["select_move", "compare_moves"]
_Names = Annotated[tuple[ToolName, ...], Field(min_length=1, max_length=2)]


def _json_object(text: str) -> dict:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def no_constant(value):
        raise ValueError("nonfinite JSON constant")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    try:
        value = json.loads(text, object_pairs_hook=unique_keys, parse_constant=no_constant, parse_float=finite_float)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the parser limit") from exc
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


class ToolDefinition(DomainModel):
    """Immutable definition; as_function_tool returns a fresh transport dictionary."""
    tool_schema_version: Literal["scrabble-tools-v1"] = TOOL_SCHEMA_VERSION
    type: Literal["function"] = "function"
    name: ToolName
    description: Annotated[str, Field(min_length=1, max_length=2048)]
    strict: bool = True
    parameters_json: Annotated[str, Field(min_length=1, max_length=65_536)]

    @model_validator(mode="after")
    def strict_root_object(self) -> Self:
        if len(self.parameters_json.encode("utf-8")) > 65_536:
            raise ValueError("tool schema exceeds its byte ceiling")
        parameters = _json_object(self.parameters_json)
        properties, required = parameters.get("properties"), parameters.get("required")
        if (not self.strict or parameters.get("type") != "object"
                or parameters.get("additionalProperties") is not False
                or not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(name, str) for name in required)
                or len(set(required)) != len(required) or set(required) != set(properties)):
            raise ValueError("strict tool definitions need a closed object with every field required")
        return self

    def as_function_tool(self) -> dict:
        checked = type(self).model_validate(self)
        return {"type": checked.type, "name": checked.name, "description": checked.description,
                "strict": checked.strict, "parameters": _json_object(checked.parameters_json)}


@lru_cache(maxsize=1)
def _definitions() -> tuple[ToolDefinition, ...]:
    payload = _json_object(files("scrabble_harness.harness").joinpath(
        "schemas", "scrabble-tools-v1.json").read_text(encoding="utf-8"))
    if (set(payload) != {"schema_version", "tool_schema_version", "tools"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["tool_schema_version"] != TOOL_SCHEMA_VERSION):
        raise ToolSchemaError("invalid packaged tool-schema header")
    records = []
    for tool in payload["tools"]:
        fields = dict(tool)
        parameters = fields.pop("parameters")
        records.append(ToolDefinition(**fields, parameters_json=json.dumps(
            parameters, sort_keys=True, separators=(",", ":"), allow_nan=False)))
    if tuple(record.name for record in records) != ("select_move", "compare_moves"):
        raise ToolSchemaError("packaged schema must define selection then comparison")
    return tuple(records)


def tool_definitions(names: tuple[ToolName, ...] = ("select_move", "compare_moves"), *,
                     version: str = TOOL_SCHEMA_VERSION) -> tuple[ToolDefinition, ...]:
    if version != TOOL_SCHEMA_VERSION:
        raise PresentationVersionError("unsupported tool-schema version")
    names = TypeAdapter(_Names, config={"strict": True}).validate_python(names)
    if len(set(names)) != len(names):
        raise ToolSchemaError("offered tool names must be unique")
    available = {record.name: record for record in _definitions()}
    return tuple(available[name] for name in names)


def parse_tool_arguments(name: str, arguments_json: str, *,
                         allowed_tools: tuple[ToolName, ...] = ("select_move", "compare_moves"),
                         version: str = TOOL_SCHEMA_VERSION, max_argument_bytes: int = 65_536
                         ) -> SelectAction | CompareAction:
    """Validate all required logical arguments; scope/visibility belong to the host."""
    definitions = tool_definitions(allowed_tools, version=version)
    definition = next((definition for definition in definitions if definition.name == name), None)
    if definition is None:
        raise ToolSchemaError("tool is not offered in this request")
    try:
        arguments_json = TypeAdapter(str, config={"strict": True}).validate_python(arguments_json)
        maximum = TypeAdapter(Annotated[int, Field(ge=1, le=65_536)], config={"strict": True}).validate_python(max_argument_bytes)
        if len(arguments_json.encode("utf-8")) > maximum:
            raise ValueError("tool arguments exceed their byte ceiling")
        arguments = _json_object(arguments_json)
        required = _json_object(definition.parameters_json)["required"]
        if set(arguments) != set(required):
            raise ValueError("tool arguments must contain exactly the required fields")
        action_type = SelectAction if name == "select_move" else CompareAction
        arguments["kind"] = "select" if name == "select_move" else "compare"
        return action_type.model_validate_json(json.dumps(arguments, allow_nan=False))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolSchemaError("tool arguments violate the strict protocol") from exc
