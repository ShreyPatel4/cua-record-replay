"""Locator backend contract: the per-surface primitives the recorder and resolver are built on.

A surface answers "which elements match this rung" and "what is this element"; the ladder logic
above it (ordering, the one-match rule, drift, weak targets) is shared by every surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import Rung
from cua.surface.base import Element

TargetKind = Literal["input", "select", "checkbox", "clickable", "text", "other"]


class FactsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabelFact(FactsModel):
    label: str = Field(description="Visible label text, dash- and whitespace-normalized.")
    relation: Literal["wrapped", "same_row", "below"] = Field(description="Where the label sits.")


class AnchorFact(FactsModel):
    anchor_text: str = Field(description="Unique visible text near the element.")
    direction: Literal["right", "left", "below", "above"] = Field(
        description="Direction from the anchor to the element."
    )
    same_row: bool = Field(description="Whether anchor and element share a table row.")
    nth: int = Field(ge=1, description="The element's position among candidates that way.")


class NormBox(FactsModel):
    x: float = Field(ge=0, le=1, description="Left edge as a fraction of frame width.")
    y: float = Field(ge=0, le=1, description="Top edge as a fraction of frame height.")
    w: float = Field(gt=0, le=1, description="Width as a fraction of frame width.")
    h: float = Field(gt=0, le=1, description="Height as a fraction of frame height.")


class ElementFacts(FactsModel):
    tag: str = Field(description="Lowercase tag or native control type.")
    role: str | None = Field(description="Accessible role, falling back to the wrapping cell's.")
    name: str | None = Field(description="Accessible name, if the element has one.")
    own_text: str = Field(
        description="The element's visible text, dash- and whitespace-normalized."
    )
    input_type: str | None = Field(description="Input type for inputs, else null.")
    kind: TargetKind = Field(description="What kind of target this is for anchor rungs.")
    frame_path: list[str] = Field(description="Frame names from the top document down.")
    labels: list[LabelFact] = Field(description="Label candidates, most specific first.")
    anchors: list[AnchorFact] = Field(description="Anchor candidates, nearest first.")
    frame_box: NormBox | None = Field(description="Box normalized to the frame viewport.")


@runtime_checkable
class LocatorBackend(Protocol):
    def candidates(self, rung: Rung, frame_path: Sequence[str]) -> list[Element]:
        """Every visible element this rung matches now, after mapping a wrapper to its control."""
        ...

    def same_element(self, a: Element, b: Element) -> bool: ...

    def describe(self, element: Element) -> ElementFacts: ...
