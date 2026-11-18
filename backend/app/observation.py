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
    """Resolve a unique live semantic target, using position only as a tie breaker.

    Names are compared in normalised form (icon glyphs stripped) on both
    sides, so a planner label of "Search courses" still matches a page
    name that arrived with a FontAwesome glyph prefix. When several
    elements share the name, this returns None unless ``expected_bounds``
    disambiguates by position — it never guesses among duplicates.
    """
    from .grounding import normalize_accessible_name as _norm_name

    wanted = _norm_name(name).casefold() if name else ""
    viable = []
    for el in elements:
        sem = getattr(el, "semantic", None)
        ia = getattr(sem, "interactability", {}) or {}
        if not sem or not ia.get("visible") or ia.get("occluded") or ia.get("pointerEvents") == "none":
            continue
        # Review (Moodle fix): a disabled element is not a live target.
        # Counting it as a duplicate would block the loop from resolving
        # its enabled twin by name, and the extension never counts it
        # either (its ambiguity check filters unusable candidates).
        if getattr(el, "enabled", True) is False:
            continue
        if role and sem.role != role and el.role != role:
            continue
        if wanted and _norm_name(sem.semantic_name).casefold() != wanted and _norm_name(el.name).casefold() != wanted:
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
