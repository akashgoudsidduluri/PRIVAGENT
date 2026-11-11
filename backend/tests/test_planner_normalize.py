"""LLM plan normalization: interpret intent, never fail the run on shape.

Models routinely emit `press` to mean "activate this control" and occasionally
overshoot the per-plan step budget. Both are recoverable at the normalization
boundary, long before the schema gate runs.
"""
from __future__ import annotations

import pytest

from app.planner import MAX_STEPS_PER_PLAN, _normalize_raw_plan, _validate_plan


def _plan(action: dict, steps: int = 1) -> dict:
    act = {"verify": "check", "timeoutMs": 5000}
    act.update(action)
    return {
        "goal": "do the thing",
        "steps": [
            {"why": f"step {i}", "action": dict(act)}
            for i in range(steps)
        ],
    }


def test_press_with_target_becomes_click():
    action = {
        "type": "press",
        "risk": "write",
        "target": {"name": "Interstellar main theme thumbnail"},
    }

    norm = _normalize_raw_plan(_plan(action))
    act = norm["steps"][0]["action"]

    assert act["type"] == "click"
    assert act["target"] == {"name": "Interstellar main theme thumbnail"}


def test_press_with_string_target_becomes_click():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "write", "target": "nav-el-3"}))
    act = norm["steps"][0]["action"]

    assert act["type"] == "click"
    assert act["target"] == {"targetId": "nav-el-3"}


def test_system_prompt_carries_pagination_and_filter_guidance():
    """Complex-search readiness: the planner prompt must teach pagination and
    filter/dropdown operation so multi-page search flows are planned."""
    from app.planner import SYSTEM_FRAME

    for needle in (
        "PAGINATION",
        "Load more",
        "FILTERS & DROPDOWNS",
        "combobox",
    ):
        assert needle in SYSTEM_FRAME, f"SYSTEM_FRAME missing '{needle}'"


def test_click_on_pagination_target_is_valid():
    plan = _validate_plan(_plan({
        "type": "click", "risk": "write",
        "target": {"name": "Next"},
        "verify": "next results page loaded",
        "timeoutMs": 8000,
    }))
    assert plan.steps[0].action.type == "click"


def test_type_with_option_label_for_select_filter_is_valid():
    plan = _validate_plan(_plan({
        "type": "type", "risk": "write",
        "target": {"name": "Sort by"},
        "params": {"text": "Price -- Low to High", "clear": True, "submit": False},
        "verify": "sorted by price",
        "timeoutMs": 5000,
    }))
    assert plan.steps[0].action.type == "type"


def test_press_rewrite_raises_risk_to_write_floor():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "read", "target": {"targetId": "t1"}}))
    assert norm["steps"][0]["action"]["risk"] == "write"


def test_press_without_key_or_target_defaults_to_enter():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "write"}))
    act = norm["steps"][0]["action"]

    assert act["type"] == "press"
    assert act["params"]["key"] == "Enter"


def test_press_with_null_key_and_no_target_defaults_to_enter():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "write", "params": {"key": None}}))
    act = norm["steps"][0]["action"]

    assert act["type"] == "press"
    assert act["params"]["key"] == "Enter"


def test_press_with_blank_key_and_target_becomes_click():
    norm = _normalize_raw_plan(
        _plan({"type": "press", "risk": "write", "target": {"name": "Play"}, "params": {"key": "  "}})
    )
    assert norm["steps"][0]["action"]["type"] == "click"


def test_explicit_key_press_is_untouched():
    norm = _normalize_raw_plan(
        _plan({"type": "press", "risk": "write", "target": {"name": "Search"}, "params": {"key": "Enter"}})
    )
    act = norm["steps"][0]["action"]

    assert act["type"] == "press"
    assert act["params"]["key"] == "Enter"


def test_empty_target_object_is_not_a_click():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "write", "target": {}}))
    act = norm["steps"][0]["action"]

    assert act["type"] == "press"
    assert act["params"]["key"] == "Enter"


def test_oversized_plan_is_clamped_instead_of_rejected():
    raw = _plan({"type": "navigate", "risk": "write", "params": {"url": "https://example.com"}},
                steps=MAX_STEPS_PER_PLAN + 2)

    norm = _normalize_raw_plan(raw)
    assert len(norm["steps"]) == MAX_STEPS_PER_PLAN

    plan = _validate_plan(raw)  # must not raise PLAN_INVALID
    assert len(plan.steps) == MAX_STEPS_PER_PLAN


def test_clamped_plan_keeps_the_leading_steps():
    raw = {
        "goal": "go",
        "steps": [
            {"why": f"step {i}", "action": {
                "type": "navigate", "risk": "write", "verify": "ok", "timeoutMs": 5000,
                "params": {"url": f"https://example.com/{i}"},
            }}
            for i in range(MAX_STEPS_PER_PLAN + 1)
        ],
    }

    plan = _validate_plan(raw)
    urls = [s.action.params.url for s in plan.steps]

    assert urls == [f"https://example.com/{i}" for i in range(MAX_STEPS_PER_PLAN)]


def test_rewritten_click_is_validated_as_a_click():
    """The rewritten action must pass the click target gate, not the press gate."""
    plan = _validate_plan(_plan({"type": "press", "risk": "write", "target": {"targetId": "t1"}}))
    assert plan.steps[0].action.type == "click"


def test_targetless_press_still_validates():
    plan = _validate_plan(_plan({"type": "press", "risk": "write"}))
    assert plan.steps[0].action.params.key == "Enter"


def test_unknown_action_type_still_rejected():
    with pytest.raises(ValueError, match="outside whitelist"):
        _validate_plan(_plan({"type": "run_js", "risk": "read"}))


def test_timeout_zero_sanitized_to_default():
    plan = _validate_plan(_plan({"type": "navigate", "risk": "write", "timeoutMs": 0, "params": {"url": "https://example.com"}}))
    assert plan.steps[0].action.timeout_ms == 10000


def test_click_with_top_level_name_lifted_to_target():
    plan = _validate_plan(_plan({"type": "click", "risk": "write", "name": "Search for Products"}))
    assert plan.steps[0].action.target["name"] == "Search for Products"


def test_click_with_target_text_mapped_to_name():
    plan = _validate_plan(_plan({"type": "click", "risk": "write", "target": {"text": "Jailer Box Office"}}))
    assert plan.steps[0].action.target["name"] == "Jailer Box Office"


def test_click_with_params_query_fallback():
    plan = _validate_plan(_plan({"type": "click", "risk": "write", "params": {"query": "Interstellar main theme"}}))
    assert plan.steps[0].action.target["name"] == "Interstellar main theme"


def test_type_without_target_defaults_to_active_element():
    plan = _validate_plan(_plan({"type": "type", "risk": "write", "params": {"text": "hello world"}}))
    assert plan.steps[0].action.target["name"] == "activeElement"


# --- Targetless clicks: the live "PLAN_INVALID after 1 step" failure -------

def test_click_derives_target_from_quoted_step_why():
    """`why` belongs to the step; reading it from the action was a dead branch."""
    raw = {
        "goal": "open a chat",
        "steps": [{
            "why": 'Click "Mummy" in the chat list',
            "action": {"type": "click", "risk": "write", "verify": "chat opened", "timeoutMs": 5000},
        }],
    }
    plan = _validate_plan(raw)
    assert plan.steps[0].action.target["name"] == "Mummy"


def test_click_derives_target_from_params_selector():
    plan = _validate_plan(
        _plan({"type": "click", "risk": "write", "params": {"selector": "Search"}})
    )
    assert plan.steps[0].action.target["name"] == "Search"


def test_later_underspecified_step_is_dropped_not_the_whole_plan():
    raw = {
        "goal": "search and play",
        "steps": [
            {"why": "search", "action": {
                "type": "type", "risk": "write", "verify": "typed", "timeoutMs": 5000,
                "target": {"name": "Search"}, "params": {"text": "Interstellar", "submit": True},
            }},
            {"why": "click the first video", "action": {
                "type": "click", "risk": "write", "verify": "video opened", "timeoutMs": 5000,
                "target": {"role": "link"},
            }},
        ],
    }
    plan = _validate_plan(raw)
    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "type"


def test_underspecified_first_step_still_raises():
    raw = {
        "goal": "play",
        "steps": [{"why": "click the first video", "action": {
            "type": "click", "risk": "write", "verify": "opened", "timeoutMs": 5000,
            "target": {"role": "link"},
        }}],
    }
    with pytest.raises(ValueError, match="requires a target"):
        _validate_plan(raw)


def test_role_only_target_resolves_when_the_page_has_one_match():
    from app.contracts import BrowserObservation
    from app.privacy import classify_and_redact

    obs = BrowserObservation.model_validate({
        "tabId": 1, "url": "https://www.youtube.com/", "title": "YouTube",
        "interactive": [{
            "targetId": "nav-el-7", "role": "button", "name": "Search", "tag": "button",
            "visible": True, "enabled": True,
            "bounds": {"x": 10, "y": 10, "width": 60, "height": 30},
        }],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}}, "dpr": 1.0,
    })
    payload, _ = classify_and_redact(obs, instruction="search youtube")

    raw = {
        "goal": "search",
        "steps": [{"why": "submit", "action": {
            "type": "click", "risk": "write", "verify": "results", "timeoutMs": 5000,
            "target": {"role": "button"},
        }}],
    }
    plan = _validate_plan(raw, payload=payload)
    assert plan.steps[0].action.target["name"] == "Search"

