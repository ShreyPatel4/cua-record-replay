"""Model client: one Anthropic Messages call per turn, returned to the loop as plain data.

The only module that imports the Anthropic SDK. SDK retries are off; the loop retries exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import anthropic

# Statuses worth one more try: timeout, conflict, rate limit, overloaded, and server errors.
_TRANSIENT_STATUS = frozenset({408, 409, 429, 529})


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: object


@dataclass(frozen=True)
class ModelTurn:
    """What the model said this turn. content is the assistant message to append verbatim."""

    text: str
    tool_uses: list[ToolUse]
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    content: list[dict[str, Any]] = field(default_factory=list)


class TransientModelError(RuntimeError):
    """A connection problem, timeout, rate limit, or server error: worth exactly one retry."""


class ModelError(RuntimeError):
    """A request the API refused for good: bad request, auth, permissions."""


class ModelClient(Protocol):
    model: str

    def complete(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn: ...


def turn_from_message(message: Any) -> ModelTurn:
    texts: list[str] = []
    uses: list[ToolUse] = []
    content: list[dict[str, Any]] = []
    for block in message.content:
        if block.type == "text":
            texts.append(block.text)
            content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            uses.append(ToolUse(id=block.id, name=block.name, input=block.input))
            content.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    usage = message.usage
    return ModelTurn(
        text="\n".join(t.strip() for t in texts if t.strip()),
        tool_uses=uses,
        stop_reason=str(message.stop_reason),
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        content=content,
    )


class AnthropicModel:
    def __init__(
        self, api_key: str, model: str, *, max_tokens: int = 1024, timeout_s: float = 90.0
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout_s)

    def complete(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=cast(Any, tools),
                # auto, not any: forcing a tool prefills the reply, so the model never writes the
                # sentence the run log keeps as its rationale. The loop answers a reply with no
                # tool call by asking for one.
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=cast(Any, messages),
            )
        except anthropic.APIConnectionError as exc:
            raise TransientModelError(f"connection failed: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            detail = f"HTTP {exc.status_code}: {type(exc).__name__}"
            if exc.status_code in _TRANSIENT_STATUS or exc.status_code >= 500:
                raise TransientModelError(detail) from exc
            raise ModelError(detail) from exc
        return turn_from_message(message)
