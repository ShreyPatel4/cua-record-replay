"""Discovery tool parsing: every tool validates strictly, and bad calls become errors, not crashes.

The same models generate the schemas sent to the model, so the two cannot drift apart.
"""

from __future__ import annotations

import pytest

from cua.discover.tools import (
    ClickCall,
    DeclareOutputCall,
    DoneCall,
    PressKeyCall,
    ToolError,
    TypeTextCall,
    parse_tool_call,
    tool_definitions,
)


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("click", {"ref": "f2e14"}, ClickCall),
        ("click", {"ref": "c3", "dialog": "accept"}, ClickCall),
        ("type_text", {"ref": "f2e14", "text": "{{secrets.operator_id}}"}, TypeTextCall),
        ("press_key", {"key": "Enter"}, PressKeyCall),
        (
            "declare_output",
            {"name": "savings_balance", "ref": "f2e37", "type": "money"},
            DeclareOutputCall,
        ),
        ("done", {"summary": "read it", "checkpoint_ref": "f2e37"}, DoneCall),
    ],
)
def test_valid_calls_parse_into_typed_models(
    name: str, arguments: dict[str, object], expected: type
) -> None:
    assert isinstance(parse_tool_call(name, arguments), expected)


@pytest.mark.parametrize(
    ("name", "arguments", "fragment"),
    [
        ("teleport", {}, "unknown tool"),
        ("click", {"ref": "[f2e14]"}, "ref"),
        ("click", {"ref": "f2e14", "x": 10}, "x"),
        ("click", "f2e14", "JSON object"),
        ("click", {"ref": "f2e14", "tool": "give_up"}, "tool"),
        ("press_key", {"key": "F5"}, "key"),
        ("scroll", {"direction": "down", "amount": 99}, "amount"),
        ("type_text", {"ref": "f2e14"}, "text"),
        ("declare_output", {"name": "Savings", "ref": "f2e37", "type": "money"}, "name"),
        ("declare_output", {"name": "x", "ref": "f2e37", "type": "float"}, "type"),
    ],
)
def test_malformed_calls_become_tool_errors_the_model_can_read(
    name: str, arguments: object, fragment: str
) -> None:
    parsed = parse_tool_call(name, arguments)
    assert isinstance(parsed, ToolError)
    assert fragment in parsed.message


def test_schemas_come_from_the_parsing_models() -> None:
    definitions = {d["name"]: d for d in tool_definitions()}
    assert set(definitions) == {
        "click",
        "type_text",
        "select_option",
        "press_key",
        "scroll",
        "navigate",
        "read_text",
        "declare_output",
        "request_confirmation",
        "done",
        "give_up",
    }
    for definition in definitions.values():
        schema = definition["input_schema"]
        assert "tool" not in schema["properties"]
        assert schema["additionalProperties"] is False
        assert definition["description"]
    assert definitions["press_key"]["input_schema"]["properties"]["key"]["enum"] == [
        "Enter",
        "Tab",
        "Escape",
    ]
    assert definitions["done"]["input_schema"]["required"] == ["summary", "checkpoint_ref"]
