"""Accessibility-tree grounding (Task 7).

The planner used to target elements by free-text accessible name (or an
author ``targetId`` that is often empty), so a rename, a duplicate label,
or a re-render could send a click into the wrong control. Grounding gives
every element in one observation a stable reference id (``e0``, ``e1``,
...) that the planner quotes verbatim; the loop resolves the ref against
the *current* observation just before execution and refuses to act when
the ref is gone or the element changed underneath it.

What the tree is — and is not:
- It is a DOM-derived accessibility approximation, not a CDP
  ``Accessibility.getFullAXTree`` dump. A Manifest V3 content script
  cannot attach ``chrome.debugger`` to its own tab without the debug
  banner and the ``debugger`` permission, so the extension builds the
  tree from roles, accessible names, states, and bounds it already
  collects (see ``extension/lib/content.ts`` and
  ``extension/src/observer.ts``). Cross-origin iframes stay opaque:
  their internals never appear in the tree.
- Refs are scoped to ONE observation. A ref from an earlier observation
  is meaningless after a navigation or re-render; the planner always
  quotes refs from the accessibility tree in the prompt it just read.
- The tree is built from the *redacted* element list, so a ref never
  carries a secret the privacy layer already stripped (password values
  are ``None`` before grounding runs).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

logger = logging.getLogger("navigator.grounding")

ELEMENT_REF_RE = re.compile(r"^e\d+$")

# Icon glyphs that leak into accessible names: icon fonts (FontAwesome,
# Material Icons, ...) render their glyphs from Unicode private-use
# areas, so a button's accessible name arrives as " Search courses"
# (U+F002 + text) instead of "Search courses". A name-only lookup for
# "Search courses" then misses, and a plan quoting the glyph never
# matches a human label. Strip them from every *matching* name; the
# un-stripped text is kept in AxNode.raw_name for debugging only.
# Ranges: PUA U+E000–U+F8FF, PUA-A U+F0000–U+FFFFD, PUA-B
# U+100000–U+10FFFD, variation selectors U+FE00–U+FE0F.
_ICON_GLYPH_RE = re.compile(
    r"[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd\ufe00-\ufe0f]"
)


def strip_icon_glyphs(text: str) -> str:
    """Remove icon-font / private-use glyphs from *text*.

    Normal text is untouched; only private-use and variation-selector
    characters are removed, and the whitespace left behind is collapsed.
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    return re.sub(r"\s+", " ", _ICON_GLYPH_RE.sub("", text)).strip()


def normalize_accessible_name(text: str) -> str:
    """The canonical form of an accessible name for matching.

    Icon glyphs stripped, whitespace collapsed, surrounding space
    removed. Both sides of every name comparison (planner target,
    tree node, semantic match) must go through this function, or a
    glyph on one side silently breaks an otherwise exact match.
    """
    return strip_icon_glyphs(text)

# A target name that looks like a raw CSS selector rather than a human
# label. Models that invent selectors emit "#search-btn", ".primary",
# "[data-testid=...]", "div > button" — none of those is an accessible
# name, and resolving one by name would silently match the wrong thing.
_SELECTOR_LIKE_RE = re.compile(
    r"^\s*[#.[]"
    r"|:\b(?:nth-child|nth-of-type|first-child|last-child|only-child|not|has|is|where)\b"
    r"|::(?:before|after|first-line|first-letter)\b"
    r"|\s[>+~]\s"
    r"|\[[^\]]*=[^\]]*\]"
)


def selector_like(text: str) -> bool:
    """True when *text* looks like a CSS selector, not a human label."""
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_SELECTOR_LIKE_RE.search(text))


def is_valid_ref(ref: Any) -> bool:
    return isinstance(ref, str) and bool(ELEMENT_REF_RE.match(ref))


@dataclass
class AxNode:
    """One grounded element: ref + identity + where to find it live."""

    ref: str
    role: str
    name: str
    tag: str = ""
    input_type: Optional[str] = None
    value: Optional[str] = None
    # Identity of the live element, for the executor's registry.
    target_id: str = ""
    css_selector: str = ""
    xpath: str = ""
    # State the observer reported (all optional; absent = unknown).
    disabled: bool = False
    checked: Optional[bool] = None
    selected: Optional[bool] = None
    expanded: Optional[bool] = None
    bounds: Optional[dict[str, float]] = None
    href: Optional[str] = None
    # "dom-ax" (extension-built tree) or "legacy" (derived from an old
    # observation that carried no axTree of its own).
    source: str = "dom-ax"
    # The accessible name before icon-glyph stripping, when it differs
    # from ``name``. Debugging aid only — never used for matching.
    raw_name: Optional[str] = None


@dataclass
class AxTree:
    nodes: list[AxNode] = field(default_factory=list)
    source: str = "dom-ax"
    # True when the observation held more nodes than MAX_AX_NODES and the
    # tail was dropped (interactive + visible first, by observation order
    # — the privacy layer already relevance-sorts the list).
    truncated: bool = False
    total: int = 0

    def by_ref(self, ref: str) -> Optional[AxNode]:
        for node in self.nodes:
            if node.ref == ref:
                return node
        return None


def _el_field(el: Any, name: str, default: Any = None) -> Any:
    if isinstance(el, dict):
        return el.get(name, default)
    attr = {
        "targetId": "target_id",
        "inputType": "input_type",
        "cssSelector": "css_selector",
        "elementRef": "element_ref",
    }.get(name, name)
    return getattr(el, attr, default)


def _prunable(el: Any) -> bool:
    """True when the element must not enter the tree.

    The extension already prunes hidden/off-screen/zero-size nodes, but
    the backend re-checks defensively: a stale or hand-built observation
    must not smuggle an invisible element into the planner's target list.
    Hidden form inputs (``type=hidden`` — e.g. Moodle's login-token
    field) are pruned here too: they never appear in the tree, are
    never validated or required, and a native form submit carries them
    automatically, so the agent must never try to type into one.
    """
    if str(_el_field(el, "inputType", "") or "").lower() == "hidden":
        return True
    if _el_field(el, "visible", True) is False:
        return True
    bounds = _el_field(el, "bounds")
    if isinstance(bounds, dict):
        try:
            w = float(bounds.get("width") or 0)
            h = float(bounds.get("height") or 0)
        except (TypeError, ValueError):
            return True
        if w <= 0 or h <= 0:
            return True
    return False


def _xpath_for(el: Any) -> str:
    """Best-effort XPath fallback for the locator pair.

    Only built from a stable author id (``//*[@id="..."]``); anything else
    would be a positional XPath that breaks on the next re-render, so it
    is left empty rather than invented.
    """
    tid = str(_el_field(el, "targetId", "") or "")
    if tid and not tid.startswith("nav-el-"):
        safe = tid.replace('"', "")
        return f'//*[@id="{safe}"]'
    return ""


def build_ax_tree(
    interactive: list[Any] | None,
    *,
    max_nodes: int = 300,
    source: str = "dom-ax",
) -> AxTree:
    """Build the grounded tree from a (redacted) element list.

    Refs are assigned in list order (``e0``, ``e1``, ...) — the privacy
    layer already relevance-sorts the list, so the cap drops the least
    relevant tail first. Ref assignment is deterministic for a given
    observation, which is what makes a ref resolvable later.
    """
    nodes: list[AxNode] = []
    total = 0
    prunable = 0
    for el in interactive or []:
        total += 1
        if _prunable(el):
            prunable += 1
            continue
        if len(nodes) >= max_nodes:
            continue
        raw_name = str(_el_field(el, "name", "") or "")
        # Names are normalised for matching on both sides of every
        # comparison (planner target, tree node, semantic match). The
        # pre-normalisation text is kept as raw_name for debugging.
        name = normalize_accessible_name(raw_name)
        role = str(_el_field(el, "role", "") or "")
        bounds = _el_field(el, "bounds")
        safe_bounds: Optional[dict[str, float]] = None
        if isinstance(bounds, dict):
            try:
                safe_bounds = {
                    k: float(bounds[k])
                    for k in ("x", "y", "width", "height")
                    if k in bounds
                }
            except (TypeError, ValueError):
                safe_bounds = None
        # State: prefer an explicit ax state block, else derive from the
        # element's own fields (disabled / aria-derived flags).
        state = _el_field(el, "state") or {}
        if not isinstance(state, dict):
            state = {}
        checked = state.get("checked")
        selected = state.get("selected")
        expanded = state.get("expanded")
        disabled = bool(state.get("disabled", not _el_field(el, "enabled", True)))
        nodes.append(
            AxNode(
                ref=f"e{len(nodes)}",
                role=role,
                name=name,
                raw_name=raw_name if raw_name != name else None,
                tag=str(_el_field(el, "tag", "") or ""),
                input_type=_el_field(el, "inputType"),
                value=_el_field(el, "value"),
                target_id=str(_el_field(el, "targetId", "") or ""),
                css_selector=str(_el_field(el, "cssSelector", "") or ""),
                xpath=_xpath_for(el),
                disabled=disabled,
                checked=bool(checked) if checked is not None else None,
                selected=bool(selected) if selected is not None else None,
                expanded=bool(expanded) if expanded is not None else None,
                bounds=safe_bounds,
                href=_el_field(el, "href"),
                source=source,
            )
        )
    truncated = (total - prunable) > len(nodes)
    if truncated:
        logger.warning(
            "grounding: ax tree truncated to %d nodes (observation held %d)",
            len(nodes),
            total,
        )
    return AxTree(nodes=nodes, source=source, truncated=truncated, total=total)


def name_counts(tree: AxTree | list[AxNode]) -> dict[str, int]:
    """Normalised-name -> node count, for ambiguity detection.

    Keys are ``normalize_accessible_name(name).casefold()``. A count
    above 1 means a name-only target is ambiguous: the planner must
    quote a ref, and the loop/extension must not guess which of the
    same-named elements the operator meant. Accepts a tree or a bare
    node list (the planner holds ``_tree_nodes``).

    Disabled nodes are NOT counted: the extension only counts usable
    (enabled) candidates, so a name shared by one enabled and one
    disabled element is unambiguous — the disabled twin can never be
    the target of a click or a type.
    """
    counts: dict[str, int] = {}
    nodes = tree.nodes if isinstance(tree, AxTree) else tree
    for node in nodes:
        if getattr(node, "disabled", False):
            continue
        key = normalize_accessible_name(node.name).casefold()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def name_role_counts(tree: AxTree | list[AxNode]) -> dict[tuple[str, str], int]:
    """(Normalised name, role) -> node count, for role-narrowed targets.

    The extension narrows an ambiguous name by the supplied role
    (Moodle's search textbox and search button share the name
    "Search courses" but have different roles), so the validator must
    count the same way: a name+role pair shared by exactly one usable
    node is unambiguous. Disabled nodes are excluded, as in
    :func:`name_counts`.
    """
    counts: dict[tuple[str, str], int] = {}
    nodes = tree.nodes if isinstance(tree, AxTree) else tree
    for node in nodes:
        if getattr(node, "disabled", False):
            continue
        key = normalize_accessible_name(node.name).casefold()
        if not key:
            continue
        role = str(getattr(node, "role", "") or "").casefold()
        counts[(key, role)] = counts.get((key, role), 0) + 1
    return counts


def normalize_ax_node(node: AxNode) -> AxNode:
    """Return *node* with its name in canonical (glyph-stripped) form.

    Used by the privacy layer for a tree the extension shipped: the
    extension strips glyphs at collection time, but a legacy or
    hand-built observation can still carry a glyph-prefixed name, and
    the tree the planner reads must match on the same form the
    validator checks. The original text survives in ``raw_name``.
    Works on both the grounding dataclass and the Pydantic contract
    model (same attribute names).
    """
    raw = node.name or ""
    name = normalize_accessible_name(raw)
    existing_raw = getattr(node, "raw_name", None)
    if name == raw and existing_raw is None:
        return node
    raw_name = existing_raw or (raw if raw != name else None)
    if hasattr(node, "model_copy"):  # Pydantic contract model
        return node.model_copy(update={"name": name, "raw_name": raw_name})
    from dataclasses import replace

    return replace(node, name=name, raw_name=raw_name)


def format_ax_tree(tree: AxTree, *, max_lines: Optional[int] = None) -> str:
    """Compact one-line-per-element rendering for the planner prompt.

    When several nodes share one accessible name, each line is annotated
    with its position among the duplicates — ``(2 of 3)`` — so the
    planner can see the name is ambiguous and quote the ref instead of
    the bare name. Unique names render exactly as before.
    """
    counts = name_counts(tree)
    seen: dict[str, int] = {}
    lines: list[str] = []
    nodes = tree.nodes if max_lines is None else tree.nodes[:max_lines]
    for node in nodes:
        key = normalize_accessible_name(node.name).casefold()
        dup = ""
        if key and counts.get(key, 0) > 1:
            seen[key] = seen.get(key, 0) + 1
            dup = f" ({seen[key]} of {counts[key]})"
        bits = [f"[{node.ref}]", node.role or "generic"]
        bits.append(f'"{node.name}"{dup}' if node.name else '"(unnamed)"')
        state: list[str] = []
        if node.disabled:
            state.append("disabled")
        if node.checked is True:
            state.append("checked")
        if node.selected is True:
            state.append("selected")
        if node.expanded is True:
            state.append("expanded")
        if node.input_type:
            state.append(f"type={node.input_type}")
        if node.target_id:
            state.append(f"id={node.target_id}")
        if state:
            bits.append("(" + ", ".join(state) + ")")
        lines.append(" ".join(bits))
    if tree.truncated:
        lines.append(
            f"... ({tree.total} elements on the page; showing {len(nodes)} — "
            "re-observe or narrow the task if the target is missing)"
        )
    return "\n".join(lines)


def resolve_ref(tree: AxTree, ref: str) -> Optional[AxNode]:
    """Resolve a planner-quoted ref to its node, or None when unknown."""
    if not is_valid_ref(ref):
        return None
    return tree.by_ref(ref)


Freshness = Literal["ok", "changed", "stale"]


def check_freshness(node: AxNode, current: AxTree) -> Freshness:
    """Compare a resolved node against the *current* tree.

    ``ok`` — same ref, same role and name: safe to act on.
    ``changed`` — the ref exists but role, name, or stable target id
    moved: the element is probably the same control after a
    re-render; acting on it needs a logged warning, never silent
    trust.
    ``stale`` — the ref is gone: the page moved on; re-observe instead
    of clicking whatever now sits at those coordinates.

    Task 13: the target-id comparison is new. A re-render can keep
    the name and role while reassigning the element's stable id, and
    that is still a changed element. The comparison only applies
    when BOTH sides carry a target id — a node built without one
    (legacy trees, synthetic fixtures) is compared on role/name
    alone, so the old ok/changed/stale tests keep passing.
    """
    live = current.by_ref(node.ref)
    if live is None:
        return "stale"
    if (live.role or "").casefold() != (node.role or "").casefold() or (
        live.name or ""
    ).casefold() != (node.name or "").casefold():
        return "changed"
    if node.target_id and live.target_id and node.target_id != live.target_id:
        return "changed"
    return "ok"


def node_center(node: AxNode) -> Optional[dict[str, float]]:
    """Centre point of the node's bounds (CSS px), for coordinate fallback."""
    if not node.bounds:
        return None
    try:
        return {
            "x": float(node.bounds["x"]) + float(node.bounds.get("width", 0)) / 2,
            "y": float(node.bounds["y"]) + float(node.bounds.get("height", 0)) / 2,
        }
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "AxNode",
    "AxTree",
    "ELEMENT_REF_RE",
    "build_ax_tree",
    "check_freshness",
    "format_ax_tree",
    "is_valid_ref",
    "name_counts",
    "name_role_counts",
    "node_center",
    "normalize_accessible_name",
    "normalize_ax_node",
    "resolve_ref",
    "selector_like",
    "strip_icon_glyphs",
]
