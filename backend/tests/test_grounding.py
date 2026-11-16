"""Task 7 — perception: AX-tree grounding, element refs, limits, OCR.

Grounding must be honest about what it proves: refs are scoped to one
observation, the tree is a DOM-derived approximation (not CDP), and a
stale ref fails instead of clicking the wrong control.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from app import config as config_mod
from app.contracts import BrowserObservation, InteractiveElement
from app.grounding import (
    AxNode,
    AxTree,
    build_ax_tree,
    check_freshness,
    format_ax_tree,
    is_valid_ref,
    resolve_ref,
    selector_like,
)
from app.ocr import extract_ocr_text, ocr_available
from app.planner import _validate_plan, build_prompt
from app.privacy import classify_and_redact


def _el(target_id: str, role: str, name: str, **kw) -> InteractiveElement:
    data = {
        "targetId": target_id, "role": role, "name": name, "tag": "button",
        "visible": True, "enabled": True,
        "bounds": {"x": 10, "y": 10, "width": 80, "height": 30},
    }
    data.update(kw)
    return InteractiveElement.model_validate(data)


def _obs(elements, **kw) -> BrowserObservation:
    data = {
        "tabId": 1, "url": "https://example.com/", "title": "T",
        "interactive": elements, "scroll": {}, "dpr": 1,
    }
    data.update(kw)
    return BrowserObservation.model_validate(data)


def _payload(elements, instruction: str = "do the thing", **kw):
    return classify_and_redact(_obs(elements, **kw), instruction=instruction)[0]


def _plan(action: dict) -> dict:
    return {"goal": "g", "steps": [{"why": "w", "action": action}]}


def _click(target: dict) -> dict:
    return {"type": "click", "risk": "write", "verify": "v", "timeoutMs": 5000, "target": target}


# --- AX tree construction -------------------------------------------------

def test_tree_assigns_stable_unique_refs_in_order():
    els = [_el("s", "textbox", "Search"), _el("b", "button", "Go")]
    tree = build_ax_tree(els)
    assert [n.ref for n in tree.nodes] == ["e0", "e1"]
    assert tree.nodes[0].role == "textbox" and tree.nodes[0].name == "Search"
    assert tree.nodes[1].target_id == "b"
    assert tree.total == 2 and tree.truncated is False


def test_tree_prunes_hidden_and_zero_size_nodes():
    els = [
        _el("a", "button", "Visible"),
        _el("h", "button", "Hidden", visible=False),
        _el("z", "button", "Zero", bounds={"x": 0, "y": 0, "width": 0, "height": 0}),
    ]
    tree = build_ax_tree(els)
    assert [n.name for n in tree.nodes] == ["Visible"]
    assert [n.ref for n in tree.nodes] == ["e0"]


def test_tree_cap_truncates_tail_and_flags_it(caplog):
    els = [_el(f"b{i}", "button", f"Btn {i}") for i in range(5)]
    tree = build_ax_tree(els, max_nodes=3)
    assert [n.ref for n in tree.nodes] == ["e0", "e1", "e2"]
    assert tree.truncated is True
    assert tree.total == 5


def test_tree_carries_state_and_locators():
    els = [_el("c", "checkbox", "Agree", inputType="checkbox",
               state={"checked": True}, cssSelector="#agree")]
    tree = build_ax_tree(els)
    node = tree.nodes[0]
    assert node.checked is True
    assert node.css_selector == "#agree"
    assert node.xpath == '//*[@id="c"]'


def test_tree_xpath_empty_without_author_id():
    els = [_el("nav-el-0", "button", "No id")]
    tree = build_ax_tree(els)
    assert tree.nodes[0].xpath == ""


def test_format_ax_tree_one_line_per_element():
    tree = build_ax_tree([_el("s", "textbox", "Search"), _el("b", "button", "Go", enabled=False)])
    text = format_ax_tree(tree)
    assert '[e0] textbox "Search"' in text
    assert '[e1] button "Go" (disabled' in text


def test_resolve_ref_and_validity():
    tree = build_ax_tree([_el("s", "textbox", "Search")])
    assert resolve_ref(tree, "e0") is not None
    assert resolve_ref(tree, "e9") is None
    assert resolve_ref(tree, "not-a-ref") is None
    assert is_valid_ref("e12") is True
    assert is_valid_ref("e") is False
    assert is_valid_ref(12) is False


def test_freshness_ok_changed_stale():
    tree = build_ax_tree([_el("s", "textbox", "Search"), _el("b", "button", "Go")])
    node = tree.nodes[0]
    assert check_freshness(node, tree) == "ok"
    changed = AxTree(nodes=[
        AxNode(ref="e0", role="textbox", name="Find"),
        AxNode(ref="e1", role="button", name="Go"),
    ])
    assert check_freshness(node, changed) == "changed"
    gone = AxTree(nodes=[AxNode(ref="e1", role="button", name="Go")])
    assert check_freshness(node, gone) == "stale"


def test_selector_like_detection():
    assert selector_like("#search-btn") is True
    assert selector_like(".primary") is True
    assert selector_like('[data-testid="x"]') is True
    assert selector_like("div > button") is True
    assert selector_like("Search") is False
    assert selector_like("Type a message") is False
    assert selector_like("") is False


# --- Planner validation ---------------------------------------------------

def test_planner_accepts_valid_ref():
    payload = _payload([_el("s", "textbox", "Search"), _el("b", "button", "Go")])
    plan = _validate_plan(_plan(_click({"elementRef": "e1"})), payload=payload)
    assert plan.steps[0].action.target["elementRef"] == "e1"


def test_planner_rejects_unknown_ref():
    payload = _payload([_el("b", "button", "Go")])
    with pytest.raises(ValueError, match="unknown element ref"):
        _validate_plan(_plan(_click({"elementRef": "e99"})), payload=payload)


def test_planner_rejects_malformed_ref():
    payload = _payload([_el("b", "button", "Go")])
    with pytest.raises(ValueError, match="malformed element ref"):
        _validate_plan(_plan(_click({"elementRef": "ref-7"})), payload=payload)


def test_planner_rejects_raw_css_selector_target():
    payload = _payload([_el("b", "button", "Go")])
    with pytest.raises(ValueError, match="raw CSS selector"):
        _validate_plan(_plan(_click({"cssSelector": "#go"})), payload=payload)


def test_planner_rejects_selector_like_name():
    payload = _payload([_el("b", "button", "Go")])
    with pytest.raises(ValueError, match="raw CSS selector"):
        _validate_plan(_plan(_click({"name": "#go"})), payload=payload)


def test_planner_accepts_legacy_name_target():
    # No ref: the degraded path still works — the loop re-validates the
    # name against the live page before acting.
    payload = _payload([_el("b", "button", "Go")])
    plan = _validate_plan(_plan(_click({"name": "Go"})), payload=payload)
    assert plan.steps[0].action.target["name"] == "Go"


def test_planner_ref_check_skipped_without_payload():
    # Validation driven directly (no observation) must not invent a ref set.
    plan = _validate_plan(_plan(_click({"elementRef": "e3"})))
    assert plan.steps[0].action.target["elementRef"] == "e3"


def test_prompt_carries_ax_tree_and_ref_instruction():
    payload = _payload([_el("s", "textbox", "Search"), _el("b", "button", "Go")])
    prompt = build_prompt("search for cats", payload, [])
    assert "<accessibility-tree" in prompt
    assert '[e0] textbox "Search"' in prompt
    assert "elementRef" in prompt


def test_legacy_observation_derives_tree():
    # An observation with no shipped axTree still grounds: the backend
    # derives a legacy tree from the element list.
    payload = _payload([_el("b", "button", "Go")])
    assert payload.ax_tree
    assert payload.ax_tree[0].ref == "e0"
    assert payload.ax_tree[0].source == "legacy"
    plan = _validate_plan(_plan(_click({"elementRef": "e0"})), payload=payload)
    assert plan.steps[0].action.target["elementRef"] == "e0"


# --- Limits ---------------------------------------------------------------

def test_config_defaults_raised():
    assert config_mod.MAX_ELEMENTS == 200
    assert config_mod.MAX_INTERACTIVE_ELEMENTS == 200
    assert config_mod.MAX_PAGE_TEXT_CHARS == 8000
    assert config_mod.MAX_INSTRUCTION_CHARS == 8000
    assert config_mod.MAX_AX_NODES == 300


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("PRIVAGENT_MAX_ELEMENTS", "42")
    monkeypatch.setenv("PRIVAGENT_MAX_PAGE_TEXT_CHARS", "1234")
    monkeypatch.setenv("PRIVAGENT_MAX_AX_NODES", "17")
    importlib.reload(config_mod)
    try:
        assert config_mod.MAX_ELEMENTS == 42
        assert config_mod.MAX_INTERACTIVE_ELEMENTS == 42
        assert config_mod.MAX_PAGE_TEXT_CHARS == 1234
        assert config_mod.MAX_AX_NODES == 17
    finally:
        monkeypatch.delenv("PRIVAGENT_MAX_ELEMENTS", raising=False)
        monkeypatch.delenv("PRIVAGENT_MAX_PAGE_TEXT_CHARS", raising=False)
        monkeypatch.delenv("PRIVAGENT_MAX_AX_NODES", raising=False)
        importlib.reload(config_mod)


def test_page_text_truncation_warns(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(config_mod, "MAX_PAGE_TEXT_CHARS", 10, raising=False)
    obs = _obs([_el("b", "button", "Go")], pageText="x" * 100)
    with caplog.at_level(logging.WARNING, logger="navigator.privacy"):
        payload, _ = classify_and_redact(obs, instruction="go")
    assert len(payload.page_text or "") <= 10
    assert any("truncated" in r.message for r in caplog.records)


def test_element_cap_warns(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(config_mod, "MAX_INTERACTIVE_ELEMENTS", 1, raising=False)
    payload = _payload([_el("a", "button", "A"), _el("b", "button", "B")])
    with caplog.at_level(logging.WARNING, logger="navigator.privacy"):
        text = payload.to_context_block()
    assert "id=a" in text or "id=b" in text
    assert any("planner sees 1" in r.message for r in caplog.records)


# --- OCR ------------------------------------------------------------------

def test_ocr_unavailable_without_binary(monkeypatch):
    monkeypatch.setattr("app.ocr.shutil.which", lambda name: None)
    assert ocr_available() is False
    assert extract_ocr_text("data:image/jpeg;base64,aGVsbG8=", enabled=True) is None


def test_ocr_disabled_by_default():
    assert config_mod.OCR_ENABLED is False
    # Even with a screenshot present, disabled OCR yields no text.
    assert extract_ocr_text("data:image/jpeg;base64,aGVsbG8=", enabled=False) is None


def test_ocr_text_labelled_untrusted_in_prompt():
    payload = _payload([_el("b", "button", "Go")])
    payload.ocr_text = "SIGN IN  1234"
    prompt = build_prompt("go", payload, [])
    assert "<ocr-text untrusted=\"true\" source=\"screenshot-ocr\">" in prompt
    assert "SIGN IN  1234" in prompt
    assert "may be misread" in prompt


def test_no_ocr_block_without_ocr_text():
    payload = _payload([_el("b", "button", "Go")])
    assert "<ocr-text" not in build_prompt("go", payload, [])


# --- Loop: ref resolution + staleness -------------------------------------

def _run_loop_with(planner, obs):
    from app import agentLoop

    class Hub:
        def __init__(self):
            self.executed = []

        async def send_action(self, task_id, action, timeout_s=45.0):
            self.executed.append(action)
            return {"ok": True, "observation": obs}

    return asyncio.run(agentLoop.run_with_injection(
        Hub(), planner=planner, obs_factory=lambda: obs, task_id="t-grounding",
    ))


def test_loop_resolves_ref_to_live_target():
    from app import agentLoop

    executed = []
    obs = {
        "tabId": 1, "url": "https://example.com/", "title": "T",
        "interactive": [
            {"targetId": "b", "role": "button", "name": "Go", "tag": "button",
             "visible": True, "enabled": True,
             "bounds": {"x": 10, "y": 20, "width": 100, "height": 40}},
        ],
        "scroll": {}, "dpr": 1,
    }

    class Hub:
        async def send_action(self, task_id, action, timeout_s=45.0):
            executed.append(action)
            return {"ok": True, "observation": obs}

    calls = {"n": 0}

    def planner(instruction, payload, history):
        calls["n"] += 1
        if calls["n"] >= 2:
            return {"goal": "g", "steps": []}
        return {"goal": "g", "steps": [{"why": "click go",
                "action": _click({"elementRef": "e0"})}]}

    state = asyncio.run(agentLoop.run_with_injection(
        Hub(), planner=planner, obs_factory=lambda: obs, task_id="t-grounding-resolve",
    ))
    assert state["final"] == "completed"
    assert executed[0]["target"]["targetId"] == "b"
    assert executed[0]["target"]["coords"] == {"x": 60.0, "y": 40.0}
    assert state["history"][0]["grounded_ref"] == "e0"


def test_loop_stale_ref_reobserves_once_then_fails_honestly():
    from app import agentLoop

    obs_calls = {"n": 0}
    obs = {
        "tabId": 1, "url": "https://example.com/", "title": "T",
        "interactive": [
            {"targetId": "b", "role": "button", "name": "Go", "tag": "button",
             "visible": True, "enabled": True,
             "bounds": {"x": 10, "y": 20, "width": 100, "height": 40}},
        ],
        "scroll": {}, "dpr": 1,
    }

    def obs_factory():
        obs_calls["n"] += 1
        return obs

    class Hub:
        async def send_action(self, task_id, action, timeout_s=45.0):
            raise AssertionError("a stale ref must never reach the executor")

    def planner(instruction, payload, history):
        # Always quotes a ref the page does not have.
        return {"goal": "g", "steps": [{"why": "click ghost",
                "action": _click({"elementRef": "e99"})}]}

    state = asyncio.run(agentLoop.run_with_injection(
        Hub(), planner=planner, obs_factory=obs_factory, task_id="t-grounding-stale",
    ))
    assert state["final"] == "failed"
    assert "element no longer found" in (state["summary"] or "")
    assert "e99" in (state["summary"] or "")
    # One re-observe happened (initial observe + one retry), then failure.
    assert obs_calls["n"] == 2
