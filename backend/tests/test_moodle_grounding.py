"""Moodle live-check hardening: duplicate names, icon glyphs, hidden inputs.

The live check of https://moodle.vce.ac.in (2026-11-16) found the login
form fully detectable but three gaps:

1. Duplicate names — "I-SEM" eight times, each course title twice
   (image link + title link). A name-only plan must not guess.
2. Icon glyphs — the search button's name is " Search courses"
   (FontAwesome U+F002 prefix). Glyphs must not break matching.
3. Hidden inputs — Moodle's login form carries a hidden login-token
   field that never appears in the AX tree. The agent must never try
   to type into, validate, or require it.

All values here are synthetic; no live site, LLM, or credential is used.
"""
from __future__ import annotations

import asyncio

import pytest

from app import agentLoop
from app.contracts import BrowserObservation, InteractiveElement
from app.grounding import (
    build_ax_tree,
    format_ax_tree,
    name_counts,
    normalize_accessible_name,
    strip_icon_glyphs,
)
from app.observation import match_semantic_target
from app.planner import _validate_plan
from app.privacy import classify_and_redact

# FontAwesome search icon, U+F002 — the glyph that prefixes Moodle's
# "Search courses" button name. Built via chr() so the source never has
# to contain a literal private-use character.
GLYPH = chr(0xF002)
VARIATION_SELECTOR = chr(0xFE0F)


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
        "tabId": 1, "url": "https://moodle.vce.ac.in/", "title": "e-Vidya",
        "interactive": elements, "scroll": {}, "dpr": 1,
    }
    data.update(kw)
    return BrowserObservation.model_validate(data)


def _payload(elements, instruction: str = "do the thing", **kw):
    return classify_and_redact(_obs(elements, **kw), instruction=instruction)[0]


def _plan(actions: list[dict]) -> dict:
    return {"goal": "g", "steps": [{"why": "w", "action": a} for a in actions]}


def _click(target: dict) -> dict:
    return {"type": "click", "risk": "write", "verify": "v", "timeoutMs": 5000, "target": target}


def _type(target: dict, text: str) -> dict:
    return {"type": "type", "risk": "write", "verify": "v", "timeoutMs": 5000,
            "target": target, "params": {"text": text}}


def _moodle_courses() -> list[InteractiveElement]:
    """Eight "I-SEM" links + a course title on two links, like Moodle."""
    els = [_el(f"sem-{i}", "link", "I-SEM", tag="a") for i in range(8)]
    els.append(_el("img-1", "link", "Intro to Computing", tag="a"))
    els.append(_el("title-1", "link", "Intro to Computing", tag="a"))
    els.append(_el("login", "button", "Log in"))
    return els


def _moodle_login() -> list[InteractiveElement]:
    """Synthetic Moodle login form: username, password, hidden token, Log in."""
    return [
        _el("username", "textbox", "Username", tag="input", inputType="text"),
        _el("password", "textbox", "Password", tag="input", inputType="password"),
        _el("logintoken", "textbox", "", tag="input", inputType="hidden", visible=False),
        _el("loginbtn", "button", "Log in"),
    ]


def _semantic_el(target_id, role, name, x=10):
    """One element with a semantic record, for match_semantic_target."""
    return BrowserObservation.model_validate({
        "tabId": 1, "url": "https://example.org", "title": "T",
        "interactive": [{
            "targetId": target_id, "role": role, "name": name, "tag": "a",
            "visible": True, "enabled": True,
            "semantic": {"elementId": target_id, "role": role,
                         "semanticName": name, "controlType": "generic",
                         "contextText": "",
                         "interactability": {"bounds": {"x": x, "y": 30, "width": 200, "height": 40},
                                             "visible": True, "opacity": 1,
                                             "pointerEvents": "auto", "occluded": False}},
        }],
        "scroll": {"y": 0, "viewport": {"w": 800, "h": 600}}, "dpr": 1,
    }).interactive[0]


# --- Icon glyphs ----------------------------------------------------------

def test_strip_icon_glyphs_moodle_search_button():
    assert strip_icon_glyphs(f"{GLYPH} Search courses") == "Search courses"
    assert strip_icon_glyphs("Search courses") == "Search courses"
    assert normalize_accessible_name(f"{GLYPH} Search courses") == "Search courses"


def test_strip_icon_glyphs_other_private_use_ranges():
    assert strip_icon_glyphs(f"{chr(0xE001)}Home") == "Home"
    assert strip_icon_glyphs(f"{chr(0xF0000)}Menu") == "Menu"
    assert strip_icon_glyphs(f"Save{VARIATION_SELECTOR}") == "Save"


def test_strip_icon_glyphs_leaves_normal_text_untouched():
    assert strip_icon_glyphs("Cafe - 50% off!") == "Cafe - 50% off!"
    assert strip_icon_glyphs("") == ""
    assert strip_icon_glyphs("  Log in  ") == "Log in"


def test_strip_icon_glyphs_edge_cases_review():
    # Glyph in the middle of a name, several glyphs, and a name that is
    # only a glyph: the result must be the clean text (or empty), never
    # a crash and never a half-stripped name.
    assert strip_icon_glyphs(f"Search {GLYPH} courses") == "Search courses"
    assert strip_icon_glyphs(f"{GLYPH}{GLYPH} Home") == "Home"
    assert strip_icon_glyphs(f"{GLYPH}") == ""
    assert strip_icon_glyphs(f"Save {GLYPH} now") == "Save now"
    # A non-PUA emoji is NOT an icon-font glyph and must survive.
    assert strip_icon_glyphs("\U0001F50D Search") == "\U0001F50D Search"
    assert strip_icon_glyphs("Caf\u00e9 \u2615") == "Caf\u00e9 \u2615"


def test_glyph_only_name_becomes_unnamed_not_a_crash_review():
    els = [_el("g", "button", f"{GLYPH}")]
    tree = build_ax_tree(els)
    assert tree.nodes[0].name == ""
    assert tree.nodes[0].raw_name == f"{GLYPH}"
    assert '"(unnamed)"' in format_ax_tree(tree)
    assert name_counts(tree) == {}


def test_tree_normalises_glyph_name_and_keeps_raw():
    els = [_el("s", "button", f"{GLYPH} Search courses")]
    tree = build_ax_tree(els)
    assert tree.nodes[0].name == "Search courses"
    assert tree.nodes[0].raw_name == f"{GLYPH} Search courses"
    assert format_ax_tree(tree) == '[e0] button "Search courses" (id=s)'


def test_tree_raw_name_is_none_when_nothing_stripped():
    els = [_el("s", "button", "Search courses")]
    tree = build_ax_tree(els)
    assert tree.nodes[0].name == "Search courses"
    assert tree.nodes[0].raw_name is None


def test_shipped_tree_with_glyph_name_is_normalised_by_privacy_layer():
    obs = _obs([_el("s", "button", f"{GLYPH} Search courses")])
    payload = classify_and_redact(obs, instruction="search")[0]
    assert payload.ax_tree is not None
    assert payload.ax_tree[0].name == "Search courses"
    assert payload.ax_tree[0].raw_name == f"{GLYPH} Search courses"


# --- Duplicate names ------------------------------------------------------

def test_duplicate_names_are_annotated_in_the_tree():
    tree = build_ax_tree(_moodle_courses())
    counts = name_counts(tree)
    assert counts["i-sem"] == 8
    assert counts["intro to computing"] == 2
    assert counts["log in"] == 1
    rendered = format_ax_tree(tree)
    assert '"I-SEM" (1 of 8)' in rendered
    assert '"I-SEM" (8 of 8)' in rendered
    assert '"Intro to Computing" (2 of 2)' in rendered
    assert '"Log in"' in rendered and "(1 of 1)" not in rendered


def test_plan_targeting_duplicate_name_by_ref_is_accepted():
    payload = _payload(_moodle_courses())
    plan = _validate_plan(_plan([_click({"elementRef": "e2"})]), payload=payload)
    assert len(plan.steps) == 1


def test_plan_targeting_duplicate_name_without_ref_is_rejected():
    payload = _payload(_moodle_courses())
    with pytest.raises(ValueError) as exc:
        _validate_plan(_plan([_click({"name": "I-SEM"})]), payload=payload)
    assert "ambiguous" in str(exc.value)
    assert "8 elements" in str(exc.value)
    assert "I-SEM" in str(exc.value)


def test_plan_targeting_duplicate_course_title_without_ref_is_rejected():
    payload = _payload(_moodle_courses())
    with pytest.raises(ValueError) as exc:
        _validate_plan(_plan([_click({"name": "Intro to Computing"})]), payload=payload)
    assert "2 elements" in str(exc.value)


def test_plan_targeting_unique_name_without_ref_is_accepted():
    payload = _payload(_moodle_courses())
    plan = _validate_plan(_plan([_click({"name": "Log in"})]), payload=payload)
    assert len(plan.steps) == 1


def test_ambiguous_name_check_is_skipped_without_an_observation():
    # Unit tests driving validation directly (payload=None) keep the old
    # behaviour: no observation means no duplicate count to check.
    plan = _validate_plan(_plan([_click({"name": "I-SEM"})]))
    assert len(plan.steps) == 1


def test_glyph_prefixed_plan_name_matches_stripped_tree_name():
    payload = _payload([_el("s", "button", "Search courses")])
    plan = _validate_plan(
        _plan([_click({"name": f"{GLYPH} Search courses"})]), payload=payload
    )
    assert len(plan.steps) == 1


def test_semantic_match_refuses_duplicate_names_without_position():
    els = [_semantic_el(f"c{i}", "link", "I-SEM", x=10 + i * 100) for i in range(3)]
    assert match_semantic_target(els, name="I-SEM") is None


def test_semantic_match_resolves_duplicate_by_position():
    els = [_semantic_el(f"c{i}", "link", "I-SEM", x=10 + i * 100) for i in range(3)]
    near = {"x": 215, "y": 45, "width": 200, "height": 40}
    assert match_semantic_target(els, name="I-SEM", expected_bounds=near).target_id == "c2"


def test_semantic_match_normalises_glyph_names():
    el = _semantic_el("s", "button", f"{GLYPH} Search courses")
    assert match_semantic_target([el], name="Search courses").target_id == "s"


# --- Hidden inputs --------------------------------------------------------

def test_hidden_input_excluded_from_ax_tree():
    tree = build_ax_tree(_moodle_login())
    assert [n.role for n in tree.nodes] == ["textbox", "textbox", "button"]
    assert [n.name for n in tree.nodes] == ["Username", "Password", "Log in"]
    assert all(n.input_type != "hidden" for n in tree.nodes)
    assert len(tree.nodes) == 3  # total counts raw inputs (4); nodes are pruned


def test_hidden_input_excluded_from_shipped_tree():
    payload = _payload(_moodle_login())
    assert payload.ax_tree is not None
    assert len(payload.ax_tree) == 3
    assert all(n.input_type != "hidden" for n in payload.ax_tree)


def test_login_plan_passes_validation_with_hidden_field_present():
    payload = _payload(_moodle_login())
    plan = _validate_plan(_plan([
        _type({"elementRef": "e0"}, "student"),
        _type({"elementRef": "e1"}, "hunter2"),
        _click({"elementRef": "e2"}),
    ]), payload=payload)
    assert len(plan.steps) == 3


def test_typing_into_hidden_field_ref_is_rejected_as_unknown():
    payload = _payload(_moodle_login())
    with pytest.raises(ValueError) as exc:
        _validate_plan(_plan([_type({"elementRef": "e3"}, "token")]), payload=payload)
    assert "unknown element ref" in str(exc.value)


def test_login_plan_by_unique_names_passes_with_hidden_field_present():
    payload = _payload(_moodle_login())
    plan = _validate_plan(_plan([
        _type({"name": "Username"}, "student"),
        _type({"name": "Password"}, "hunter2"),
        _click({"name": "Log in"}),
    ]), payload=payload)
    assert len(plan.steps) == 3


def test_login_plan_passes_with_hidden_field_absent_review():
    # The same login flow must work when the page has no hidden token
    # field at all — the agent must not require one.
    els = [e for e in _moodle_login() if e.input_type != "hidden"]
    payload = _payload(els)
    plan = _validate_plan(_plan([
        _type({"elementRef": "e0"}, "student"),
        _type({"elementRef": "e1"}, "hunter2"),
        _click({"elementRef": "e2"}),
    ]), payload=payload)
    assert len(plan.steps) == 3


def test_hidden_input_with_bounds_and_visible_still_pruned_review():
    # Pruning keys off inputType, not off visibility or size: a hidden
    # input that (wrongly) reports visible bounds must still never
    # enter the tree.
    els = _moodle_login()
    els[2] = _el("logintoken", "textbox", "logintoken", tag="input",
                 inputType="hidden", visible=True)
    tree = build_ax_tree(els)
    assert len(tree.nodes) == 3
    assert all(n.input_type != "hidden" for n in tree.nodes)


def test_role_narrows_duplicate_name_review():
    # Moodle's own search textbox and search button share the
    # normalised name "Search courses" (roles textbox vs button). A
    # name+role target is unambiguous — the extension resolves it —
    # so validation must accept it instead of rejecting the count.
    els = [
        _el("sf", "textbox", "Search courses", tag="input", inputType="search"),
        _el("sb", "button", f"{GLYPH} Search courses"),
    ]
    payload = _payload(els)
    plan = _validate_plan(
        _plan([_click({"name": "Search courses", "role": "button"})]),
        payload=payload,
    )
    assert len(plan.steps) == 1
    # ... but a role that matches NEITHER duplicate is not a
    # disambiguator: the name is still ambiguous and must be rejected.
    with pytest.raises(ValueError) as exc:
        _validate_plan(
            _plan([_click({"name": "Search courses", "role": "link"})]),
            payload=payload,
        )
    assert "ambiguous" in str(exc.value)
    assert "2 elements" in str(exc.value)


def test_target_id_disambiguates_duplicate_name_review():
    # An author targetId is a stable identity the extension resolves
    # before name matching, so a duplicated name + targetId validates.
    payload = _payload(_moodle_courses())
    plan = _validate_plan(
        _plan([_click({"name": "I-SEM", "targetId": "sem-3"})]),
        payload=payload,
    )
    assert len(plan.steps) == 1


def test_disabled_duplicate_does_not_count_review():
    # The extension only counts usable (enabled) candidates, so the
    # validator must not count a disabled twin: one enabled + one
    # disabled element sharing a name is unambiguous.
    els = [
        _el("a", "link", "I-SEM", tag="a"),
        _el("b", "link", "I-SEM", tag="a", enabled=False),
    ]
    payload = _payload(els)
    plan = _validate_plan(_plan([_click({"name": "I-SEM"})]), payload=payload)
    assert len(plan.steps) == 1
    # Two enabled twins are still ambiguous.
    els2 = [
        _el("a", "link", "I-SEM", tag="a"),
        _el("b", "link", "I-SEM", tag="a"),
    ]
    payload2 = _payload(els2)
    with pytest.raises(ValueError) as exc:
        _validate_plan(_plan([_click({"name": "I-SEM"})]), payload=payload2)
    assert "2 elements" in str(exc.value)


def test_otp_field_with_glyph_name_is_still_blocked_review():
    # The OTP guard compares names: the planner reads the STRIPPED
    # name ("One-time code") from the AX tree, while a legacy
    # observation can still carry the glyph-prefixed element name.
    # Both sides must be normalised, or the guard misses and a type
    # into the OTP field slips past validation.
    from app.takeover import is_otp_field

    els = [
        _el("otp", "textbox", f"{GLYPH} One-time code", tag="input",
            inputType="text"),
    ]
    assert is_otp_field(els[0])
    payload = _payload(els)
    with pytest.raises(ValueError) as exc:
        _validate_plan(
            _plan([_type({"name": "One-time code"}, "123456")]),
            payload=payload,
        )
    assert "one-time-code" in str(exc.value)
    # And the reverse: a glyph-prefixed plan name against a stripped
    # element name must also be caught.
    els2 = [_el("otp", "textbox", "One-time code", tag="input", inputType="text")]
    payload2 = _payload(els2)
    with pytest.raises(ValueError) as exc:
        _validate_plan(
            _plan([_type({"name": f"{GLYPH} One-time code"}, "123456")]),
            payload=payload2,
        )
    assert "one-time-code" in str(exc.value)


def test_semantic_match_skips_disabled_duplicates_review():
    # The loop's name fallback must not treat a disabled twin as a
    # live duplicate: with one enabled and one disabled "I-SEM", the
    # enabled one resolves.
    els = [
        _semantic_el("c0", "link", "I-SEM", x=10),
        _semantic_el("c1", "link", "I-SEM", x=210),
    ]
    # _semantic_el builds enabled=True; rebuild the second as disabled.
    els[1] = BrowserObservation.model_validate({
        "tabId": 1, "url": "https://example.org", "title": "T",
        "interactive": [{
            "targetId": "c1", "role": "link", "name": "I-SEM", "tag": "a",
            "visible": True, "enabled": False,
            "semantic": {"elementId": "c1", "role": "link",
                         "semanticName": "I-SEM", "controlType": "generic",
                         "contextText": "",
                         "interactability": {"bounds": {"x": 210, "y": 30, "width": 200, "height": 40},
                                             "visible": True, "opacity": 1,
                                             "pointerEvents": "auto", "occluded": False}},
        }],
        "scroll": {"y": 0, "viewport": {"w": 800, "h": 600}}, "dpr": 1,
    }).interactive[0]
    assert match_semantic_target(els, name="I-SEM").target_id == "c0"


# --- Loop: synthetic Moodle login end to end ------------------------------

class _FakeHub:
    """Minimal extension stand-in: every action succeeds."""

    def __init__(self):
        self.executed: list[str] = []

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        self.executed.append(action["type"])
        return {"ok": True}


class _RawProvider:
    """Provider double returning one scripted raw plan per call, then empty
    plans. The loop executes only the first step of each plan and replans,
    so a multi-step flow is a sequence of one-step plans."""

    def __init__(self, plans):
        self.plans = plans
        self.calls = 0

    def plan_call(self, instruction, payload, history, working_memory=None):
        self.calls += 1
        if self.calls <= len(self.plans):
            return self.plans[self.calls - 1]
        return {"goal": "log in", "steps": []}


def _validating_planner(provider):
    from app.planner import plan_from_observation
    from app.privacy import PrivacyFilteredPayload

    def planner(instruction, payload, history, working_memory=None):
        pf = PrivacyFilteredPayload(**payload) if isinstance(payload, dict) else payload
        return plan_from_observation(
            instruction=instruction, payload=pf, provider=provider,
            history=history, working_memory=working_memory,
        ).model_dump(by_alias=True, exclude_none=True)

    return planner


def test_loop_drives_moodle_login_with_hidden_token():
    """The full loop types username/password and clicks Log in while a
    hidden login-token field is present — no error about the hidden field,
    and the token is never a plan target."""
    hub = _FakeHub()
    login_steps = [
        {"why": "enter username", "action": _type({"elementRef": "e0"}, "student")},
        {"why": "enter password", "action": _type({"elementRef": "e1"}, "hunter2")},
        {"why": "submit the form", "action": _click({"elementRef": "e2"})},
    ]
    login_plans = [
        {"goal": "log in to Moodle", "steps": [step]} for step in login_steps
    ]
    planner = _validating_planner(_RawProvider(login_plans))

    def obs_factory():
        els = [
            {"targetId": "username", "role": "textbox", "name": "Username", "tag": "input",
             "inputType": "text", "visible": True, "enabled": True,
             "bounds": {"x": 10, "y": 10, "width": 80, "height": 30}},
            {"targetId": "password", "role": "textbox", "name": "Password", "tag": "input",
             "inputType": "password", "visible": True, "enabled": True,
             "bounds": {"x": 10, "y": 50, "width": 80, "height": 30}},
            {"targetId": "logintoken", "role": "textbox", "name": "", "tag": "input",
             "inputType": "hidden", "visible": False, "enabled": True,
             "bounds": {"x": 0, "y": 0, "width": 0, "height": 0}},
            {"targetId": "loginbtn", "role": "button", "name": "Log in", "tag": "button",
             "visible": True, "enabled": True,
             "bounds": {"x": 10, "y": 90, "width": 80, "height": 30}},
        ]
        return {"tabId": 1, "url": "https://moodle.vce.ac.in/",
                "title": "e-Vidya", "interactive": els,
                "scroll": {"y": 0, "viewport": {"w": 800, "h": 600}}, "dpr": 1}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=planner, obs_factory=obs_factory,
        instruction="log in to Moodle",
    ))
    assert state["final"] == "completed"
    assert hub.executed == ["type", "type", "click"]
    assert "hidden" not in (state.get("summary") or "").lower()


def test_loop_clicks_second_duplicate_by_ref_not_first_review():
    """A ref for a duplicated name must resolve by position: clicking
    e5 (the 6th of 8 "I-SEM" links) must execute against targetId
    sem-5 — never the first match."""
    executed: list[dict] = []

    class _RecordingHub:
        async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
            executed.append(action)
            return {"ok": True}

    def obs_factory():
        els = [
            {"targetId": f"sem-{i}", "role": "link", "name": "I-SEM", "tag": "a",
             "visible": True, "enabled": True,
             "bounds": {"x": 10 + i * 100, "y": 10, "width": 80, "height": 30}}
            for i in range(8)
        ]
        return {"tabId": 1, "url": "https://moodle.vce.ac.in/",
                "title": "e-Vidya", "interactive": els,
                "scroll": {"y": 0, "viewport": {"w": 800, "h": 600}}, "dpr": 1}

    planner = _validating_planner(_RawProvider([
        {"goal": "open the 6th I-SEM link",
         "steps": [{"why": "open it", "action": _click({"elementRef": "e5"})}]},
    ]))
    state = asyncio.run(agentLoop.run_with_injection(
        _RecordingHub(), planner=planner, obs_factory=obs_factory,
        instruction="open the 6th I-SEM link",
    ))
    assert state["final"] == "completed"
    assert len(executed) == 1
    assert executed[0]["target"]["targetId"] == "sem-5"
