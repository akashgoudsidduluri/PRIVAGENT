"""Coordinate grounding for screenshot->page mapping.

All coordinates crossing a model/act boundary are CSS pixels (page space).
Screenshots captured via chrome.debugger are device pixels; the only place
device pixels exist here is .captureScreenshot scale — normalized at ingest.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Any


@dataclass(frozen=True)
class GroundedPoint:
    x: float  # CSS px, page space
    y: float


def ground_coords(
    *, device_x: float, device_y: float, dpr: float, scroll_y: float
) -> GroundedPoint:
    """Convert a device-pixel screenshot point + DPR + scroll offset into
    CSS-pixel page coordinates. dpr<=0 is a contract breach — ValueError."""
    if dpr <= 0:
        raise ValueError("dpr must be > 0")
    return GroundedPoint(x=device_x / dpr, y=device_y / dpr + scroll_y)


def match_semantic_target(elements: list[Any], *, target_id: str = "", name: str = "",
                          role: str = "", expected_bounds: dict[str, float] | None = None) -> Any | None:
    """Resolve a unique live semantic target, using position only as a tie breaker."""
    viable = []
    for el in elements:
        sem = getattr(el, "semantic", None)
        ia = getattr(sem, "interactability", {}) or {}
        if not sem or not ia.get("visible") or ia.get("occluded") or ia.get("pointerEvents") == "none":
            continue
        if role and sem.role != role and el.role != role:
            continue
        if name and sem.semantic_name.casefold() != name.casefold() and el.name.casefold() != name.casefold():
            continue
        if target_id and el.target_id == target_id:
            return el
        viable.append(el)
    if len(viable) == 1 and (name or role):
        return viable[0]
    if expected_bounds and len(viable) > 1:
        nearby = sorted(viable, key=lambda el: hypot(
            el.semantic.interactability["bounds"]["x"] - expected_bounds["x"],
            el.semantic.interactability["bounds"]["y"] - expected_bounds["y"]))
        distance = lambda el: hypot(el.semantic.interactability["bounds"]["x"] - expected_bounds["x"],
                                    el.semantic.interactability["bounds"]["y"] - expected_bounds["y"])
        if distance(nearby[0]) <= 48 and distance(nearby[1]) - distance(nearby[0]) > 24:
            return nearby[0]
    return None
