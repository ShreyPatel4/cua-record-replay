"""Discovery tools: the actions the model may request, their JSON schemas, and strict parsing.

A malformed call becomes a ToolError the loop hands back to the model; it never crashes a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from cua.vocab import KeyName

Ref = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9]{0,15}$",
        description="A ref from the latest snapshot, without the brackets, e.g. f2e14 or c3.",
    ),
]


class ToolCallModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClickCall(ToolCallModel):
    tool: Literal["click"]
    ref: Ref
    dialog: Literal["accept", "dismiss"] | None = Field(
        default=None,
        description="Only when an earlier click on this element opened a native dialog you have "
        "already seen reported: how to answer it this time.",
    )


class TypeTextCall(ToolCallModel):
    tool: Literal["type_text"]
    ref: Ref
    text: str = Field(
        min_length=1,
        max_length=200,
        description="Literal text, or one {{secrets.name}} template as the whole text.",
    )
    clear_first: bool = Field(default=True, description="Clear the field before typing.")


class SelectOptionCall(ToolCallModel):
    tool: Literal["select_option"]
    ref: Ref
    option: str = Field(min_length=1, max_length=120, description="Visible label of the option.")


class PressKeyCall(ToolCallModel):
    tool: Literal["press_key"]
    key: KeyName = Field(description="Key to press on the focused element.")


class ScrollCall(ToolCallModel):
    tool: Literal["scroll"]
    direction: Literal["up", "down"]
    amount: int = Field(default=1, ge=1, le=10, description="Wheel steps.")


class NavigateCall(ToolCallModel):
    tool: Literal["navigate"]
    url: str = Field(min_length=1, max_length=500, description="Absolute URL you have seen.")


class ReadTextCall(ToolCallModel):
    tool: Literal["read_text"]
    ref: Ref


class DeclareOutputCall(ToolCallModel):
    tool: Literal["declare_output"]
    name: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,39}$", description="snake_case name for the value."
    )
    ref: Ref = Field(description="The element showing the value itself, not its label.")
    type: Literal["money", "string", "integer", "date"]


class RequestConfirmationCall(ToolCallModel):
    tool: Literal["request_confirmation"]
    reason: str = Field(
        min_length=1, max_length=300, description="What will be created or changed."
    )


class DoneCall(ToolCallModel):
    tool: Literal["done"]
    summary: str = Field(min_length=1, max_length=500, description="What was accomplished.")
    checkpoint_ref: Ref = Field(description="The element on screen that proves the goal is met.")


class GiveUpCall(ToolCallModel):
    tool: Literal["give_up"]
    reason: str = Field(min_length=1, max_length=500, description="Why you cannot proceed.")


ToolCall = Annotated[
    ClickCall
    | TypeTextCall
    | SelectOptionCall
    | PressKeyCall
    | ScrollCall
    | NavigateCall
    | ReadTextCall
    | DeclareOutputCall
    | RequestConfirmationCall
    | DoneCall
    | GiveUpCall,
    Field(discriminator="tool"),
]
_ADAPTER: TypeAdapter[ToolCall] = TypeAdapter(ToolCall)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "click": "Click the element with this ref.",
    "type_text": "Type into a text field. Credentials go in as {{secrets.name}} templates.",
    "select_option": "Choose an option in a select control by its visible label.",
    "press_key": "Press Enter, Tab, or Escape on the focused element.",
    "scroll": "Scroll the page.",
    "navigate": "Load a URL in the top document. The allowlist decides whether it may load.",
    "read_text": "Return the visible text of an element.",
    "declare_output": "Mark the element that shows a value the goal asks you to read.",
    "request_confirmation": "Ask before any action that creates, changes, submits, or deletes.",
    "done": "Stop: the goal is met and the proof is on screen.",
    "give_up": "Stop: you cannot proceed safely. A human is asked to help.",
}
_MODELS: dict[str, type[ToolCallModel]] = {
    "click": ClickCall,
    "type_text": TypeTextCall,
    "select_option": SelectOptionCall,
    "press_key": PressKeyCall,
    "scroll": ScrollCall,
    "navigate": NavigateCall,
    "read_text": ReadTextCall,
    "declare_output": DeclareOutputCall,
    "request_confirmation": RequestConfirmationCall,
    "done": DoneCall,
    "give_up": GiveUpCall,
}
# Tools that act on the page; the others read or end the run.
ACTING_TOOLS = frozenset({"click", "type_text", "select_option", "press_key", "scroll", "navigate"})


@dataclass(frozen=True)
class ToolError:
    tool: str
    message: str


def _strip_titles(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def tool_definitions() -> list[dict[str, Any]]:
    """Anthropic tool definitions generated from the same models that parse the calls."""
    definitions = []
    for name, model in _MODELS.items():
        schema = model.model_json_schema()
        properties = {k: v for k, v in schema["properties"].items() if k != "tool"}
        definitions.append(
            {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "input_schema": {
                    "type": "object",
                    "properties": _strip_titles(properties),
                    "required": [r for r in schema.get("required", []) if r != "tool"],
                    "additionalProperties": False,
                },
            }
        )
    return definitions


def parse_tool_call(name: str, arguments: object) -> ToolCall | ToolError:
    if name not in _MODELS:
        return ToolError(name, f"unknown tool {name!r}; use one of {sorted(_MODELS)}")
    if not isinstance(arguments, dict):
        return ToolError(name, "arguments must be a JSON object")
    if "tool" in arguments:
        return ToolError(name, "unexpected argument 'tool'")
    try:
        return _ADAPTER.validate_python({**arguments, "tool": name})
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"][1:]) or "arguments"
            problems.append(f"{where}: {error['msg']}")
        return ToolError(name, "; ".join(problems))
