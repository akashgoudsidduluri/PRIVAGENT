"""Completion honesty and human handoff.

Live runs produced "Mission Completed" after a failed `type` and after ten
sign-in clicks on empty fields. The prompt now forbids that; these tests cover
the deterministic backstop that rewrites an unverifiable completion into either
one sensitive wait (CAPTCHA / login wall) or an honest failure.
"""
from __future__ import annotations

import asyncio

from app import agentLoop
from app.contracts import BrowserObservation
from app.planner import plan_from_observation, _validate_plan
from app.privacy import PrivacyFilteredPayload, classify_and_redact


def _payload(url: str, interactive: list[dict] | None = None, instruction: str = "do the task"):
    obs = BrowserObservation.model_validate({
        "tabId": 1,
        "url": url,
        "title": "Page",
        "interactive": interactive or [],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1.0,
    })
    payload, _ = classify_and_redact(obs, instruction=instruction)
    return payload


def _element(name: str, role: str = "button", tag: str = "button", input_type: str | None = None) -> dict:
    return {
        "targetId": name.lower().replace(" ", "-"),
        "role": role,
        "name": name,
        "tag": tag,
        "inputType": input_type,
        "visible": True,
        "enabled": True,
    }


def _completion(answer: str = "Done") -> dict:
    return {"goal": "finish", "steps": [], "answer": answer}


def _handoff_history() -> list[dict]:
    return [{
        "action": {
            "type": "wait",
            "risk": "sensitive",
            "verify": "waiting for user to complete CAPTCHA and log in",
            "timeoutMs": 30000,
        },
        "result_ok": True,
        "evidence": "waited for the operator",
    }]


def _captcha_page():
    return _payload("https://portal.example.edu/exam", [
        _element("I'm not a robot", role="checkbox", tag="input", input_type="checkbox"),
        _element("Sign in"),
    ])


def _whatsapp(*elements: dict):
    return _payload("https://web.whatsapp.com/", list(elements), instruction="send a message to Mummy")


# --- Issue 3: no false "Mission Completed" ----------------------------------

def test_login_page_completion_is_a_honest_failure():
    plan = _validate_plan(
        _completion("Login attempted using saved credentials"),
        payload=_payload("https://vce.ac.in/Default.aspx"),
        history=[],
        instruction="fetch my attendance",
    )
    # First sighting of a login wall hands off to the human instead of looping.
    assert plan.blocked is False
    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "wait"


def test_msg_loginrequired_is_a_login_surface():
    plan = _validate_plan(
        _completion("Attendance fetched: 92%"),
        payload=_payload("https://portal.example.edu/home?msg=loginrequired"),
        history=[],
        instruction="fetch my attendance",
    )
    assert plan.blocked is False
    assert plan.steps[0].action.type == "wait"


def test_failed_type_with_open_chat_is_honest_failure():
    composer = _element("Type a message", role="textbox", tag="div")
    plan = _validate_plan(
        _completion("Message 'babua' has been sent to Mummy on WhatsApp"),
        payload=_whatsapp(composer),
        history=[{
            "action": {"type": "type", "target": {"name": "Type a message"}},
            "result_ok": False,
            "evidence": None,
            "error": "selector not found",
        }],
        instruction="send 'babua' to Mummy on WhatsApp",
    )
    assert plan.blocked is True
    assert plan.steps == []
    assert "incomplete" in (plan.answer or "").lower()


def test_whatsapp_claim_without_open_chat_is_honest_failure():
    search = _element("Search or start new chat", role="textbox", tag="div")
    plan = _validate_plan(
        _completion("Message 'hi' has been sent to Mummy"),
        payload=_whatsapp(search),
        history=[],
        instruction="send 'hi' to Mummy on WhatsApp",
    )
    assert plan.blocked is True
    assert "no chat" in (plan.answer or "").lower()


def test_verified_whatsapp_send_still_completes():
    composer = _element("Type a message", role="textbox", tag="div")
    plan = _validate_plan(
        _completion("Message 'hi' has been sent to Mummy"),
        payload=_whatsapp(composer),
        history=[{
            "action": {"type": "type", "target": {"name": "Type a message"},
                       "params": {"text": "hi", "submit": True}},
            "result_ok": True,
            "evidence": "typed + submitted",
        }],
        instruction="send 'hi' to Mummy on WhatsApp",
    )
    assert plan.blocked is False
    assert plan.steps == []
    assert plan.answer.startswith("Message 'hi'")


def test_plans_with_steps_are_never_rewritten():
    raw = {
        "goal": "log in",
        "steps": [{
            "why": "one attempt only",
            "action": {"type": "click", "risk": "write", "verify": "clicked sign in",
                       "timeoutMs": 5000, "target": {"name": "Sign in"}},
        }],
    }
    plan = _validate_plan(raw, payload=_payload("https://vce.ac.in/Default.aspx"), history=[])
    assert plan.steps[0].action.type == "click"


def test_non_login_success_is_untouched():
    plan = _validate_plan(
        _completion("Attendance fetched: 92%"),
        payload=_payload("https://portal.example.edu/attendance"),
        history=[],
        instruction="fetch my attendance",
    )
    assert plan.steps == []
    assert plan.blocked is False
    assert plan.answer == "Attendance fetched: 92%"


# --- Issue 4: one human handoff, never repeated submits ---------------------

def test_captcha_completion_becomes_one_sensitive_wait():
    plan = _validate_plan(
        _completion("Sign in clicked"),
        payload=_captcha_page(),
        history=[],
        instruction="log in and fetch marks",
    )
    assert plan.blocked is False
    assert len(plan.steps) == 1
    act = plan.steps[0].action
    assert act.type == "wait"
    assert act.risk == "sensitive"
    assert act.timeout_ms == 30000
    assert "CAPTCHA" in act.verify
    assert "Please solve the CAPTCHA" in plan.steps[0].why


def test_login_wall_after_handoff_is_honest_failure():
    plan = _validate_plan(
        _completion("Login attempted"),
        payload=_payload("https://vce.ac.in/Default.aspx"),
        history=_handoff_history(),
        instruction="fetch my attendance",
    )
    assert plan.steps == []
    assert plan.blocked is True
    assert "incomplete" in (plan.answer or "").lower()


def test_captcha_after_handoff_is_honest_failure():
    plan = _validate_plan(
        _completion("Marks fetched"),
        payload=_captcha_page(),
        history=_handoff_history(),
        instruction="log in and fetch marks",
    )
    assert plan.blocked is True
    assert plan.steps == []


def test_gate_message_asks_the_operator_to_solve_the_captcha():
    wait = {"type": "wait", "risk": "sensitive", "verify": "waiting for user to complete CAPTCHA and log in"}
    assert agentLoop._gate_message(wait, False, False) == agentLoop.HANDOFF_MESSAGE
    assert "solve the CAPTCHA" in agentLoop.HANDOFF_MESSAGE
    assert "Continue" in agentLoop.HANDOFF_MESSAGE


def test_gate_message_prioritises_qr_login_over_generic_handoff():
    wait = {"type": "wait", "risk": "sensitive", "verify": "qr"}
    assert "QR code" in (agentLoop._gate_message(wait, True, False) or "")


# --- Wiring: a blocked completion ends as failed, not "completed" ----------

class _CompletionProvider:
    """Always answers with a success claim, ignoring the page."""

    def plan_call(self, instruction, payload, history, working_memory=None):
        return {"goal": "log in", "steps": [], "answer": "Login attempted using saved credentials"}


class _Hub:
    ext_online = True

    def __init__(self):
        self.executed: list[str] = []

    async def send_action(self, task_id, action, timeout_s=45.0):
        self.executed.append(action["type"])
        return {"ok": True}


def _guarded_planner(instruction, payload, history, working_memory=None):
    pf = PrivacyFilteredPayload(**payload) if isinstance(payload, dict) else payload
    return plan_from_observation(
        instruction=instruction, payload=pf, provider=_CompletionProvider(),
        history=history, working_memory=working_memory,
    ).model_dump(by_alias=True, exclude_none=True)


def test_loop_hands_off_once_then_reports_failure():
    hub = _Hub()
    state = asyncio.run(agentLoop.run_with_injection(
        hub,
        planner=_guarded_planner,
        obs_factory=lambda: {"tabId": 1, "url": "https://vce.ac.in/Default.aspx",
                             "title": "VCE Login", "interactive": [], "scroll": {}, "dpr": 1},
        instruction="fetch my attendance",
        task_id="task-completion-guard",
    ))
    assert state["final"] == "failed"
    assert "incomplete" in (state["summary"] or "").lower()
    # Exactly one sensitive wait: never a burst of sign-in clicks.
    assert hub.executed == ["wait"]


def test_extraction_claim_rejected_when_still_on_search_results():
    """Completion claiming article/specs extraction while still on search results is rejected."""
    plan = _validate_plan(
        _completion("Extracted neural network definition: A neural network is a..."),
        payload=_payload("https://en.wikipedia.org/w/index.php?search=neural+network"),
        history=[],
        instruction="search Wikipedia for neural networks and extract the first 2 paragraphs",
    )
    assert plan.blocked is True
    assert "cannot claim extraction from search results" in plan.answer


def test_extraction_claim_accepted_on_article_page():
    """Completion claiming article extraction on an actual article page is accepted."""
    plan = _validate_plan(
        _completion("Extracted neural network definition: A neural network is a..."),
        payload=_payload("https://en.wikipedia.org/wiki/Neural_network"),
        history=[{"action": {"type": "click"}, "result_ok": True, "evidence": "navigated to article"}],
        instruction="search Wikipedia for neural networks and extract the first 2 paragraphs",
    )
    assert plan.blocked is False
    assert "Extracted" in plan.answer
