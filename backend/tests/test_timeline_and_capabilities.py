"""Regression tests for capability awareness, strict completion verification,
event deduplication, and resilient multi-site resumption.
"""
from __future__ import annotations

import asyncio
from typing import Any
import pytest

from app.capabilities import (
    check_supported_capability,
    is_capability_query,
    format_capability_overview,
    SYSTEM_CAPABILITY_PROMPT,
)
from app.completion_verifier import (
    verify_task_completion,
    is_refusal_message,
    classify_instruction_type,
    verify_navigation_destination,
)
from app.intent_router import classify_intent, TaskIntent
from app.planner import try_deterministic_plan, plan_from_observation
from app.privacy import PrivacyFilteredPayload, classify_and_redact
from app.contracts import BrowserObservation, BrowserAction, TaskPlan
from app.hub import Hub, TaskRecord
from app.extractors.research import (
    init_multi_site_tracker,
    update_multi_site_tracker,
    is_already_resolved_site,
    get_pending_sites,
    is_multi_site_task_complete,
)


def _make_payload(url: str, title: str = "", interactive: list[dict] | None = None, instruction: str = "task") -> PrivacyFilteredPayload:
    norm_interactive = []
    for el in (interactive or []):
        item = dict(el)
        item.setdefault("visible", True)
        item.setdefault("enabled", True)
        norm_interactive.append(item)
    obs = BrowserObservation.model_validate({
        "tabId": 1,
        "url": url,
        "title": title,
        "interactive": norm_interactive,
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1.0,
    })
    payload, _ = classify_and_redact(obs, instruction=instruction)
    return payload


# ============================================================================
# 1. "Open mail" triggers browser navigation
# ============================================================================

def test_open_mail_triggers_browser_navigation():
    """Verify 'open mail', 'open email', and 'open gmail' trigger browser navigation."""
    test_queries = ["open mail", "open email", "open gmail", "check my email", "go to inbox"]
    for q in test_queries:
        classification = classify_intent(q)
        assert classification.is_browser is True, f"'{q}' should be classified as a browser task"
        assert classification.intent == TaskIntent.NEW_BROWSER_TASK

    # Verify deterministic planner directly emits navigation to mail.google.com
    payload = _make_payload("about:blank", title="", instruction="open mail")
    plan = try_deterministic_plan("open mail", payload, history=[])
    assert plan is not None, "Deterministic plan should handle 'open mail'"
    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "navigate"
    assert "mail.google.com" in plan.steps[0].action.params.url


# ============================================================================
# 2. Gmail authentication handoff
# ============================================================================

def test_gmail_authentication_handoff():
    """When navigated to Gmail and Google Sign-In is presented, agent must pause for handoff."""
    auth_url = "https://accounts.google.com/v3/signin/identifier?continue=https://mail.google.com/mail"
    payload = _make_payload(
        auth_url,
        title="Sign in - Google Accounts",
        interactive=[
            {"targetId": "e1", "role": "textbox", "name": "Email or phone", "tag": "input"},
            {"targetId": "b1", "role": "button", "name": "Next", "tag": "button"},
        ],
        instruction="open mail",
    )
    # try_deterministic_plan detects sign-in screen
    plan = try_deterministic_plan("open mail", payload, history=[{"action": {"type": "navigate"}, "result_ok": True}])
    assert plan is not None, "Login surface must trigger deterministic handoff"
    assert plan.goal == "Complete sign-in"
    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "wait"
    assert plan.steps[0].action.risk == "sensitive"
    assert "sign-in" in plan.steps[0].why.lower()


# ============================================================================
# 3. Unsupported capability handling
# ============================================================================

def test_unsupported_capability_handling():
    """Desktop OS management requests must fail honestly, never claiming completion."""
    unsupported_instructions = [
        "reboot my physical computer",
        "shut down my computer",
        "format hard drive C:",
        "hack into someone's bank account",
        "run malware on my system",
    ]
    for instr in unsupported_instructions:
        is_supp, reason = check_supported_capability(instr)
        assert is_supp is False, f"'{instr}' must be recognized as unsupported"
        assert "PrivAgent is an autonomous web browser" in reason

        # Intent router routes without attempting browser actions
        classification = classify_intent(instr)
        assert classification.is_browser is False
        assert "unsupported" in classification.reason

        # Strict completion verifier marks unsupported requests as failed
        res = verify_task_completion(instr, "completed", reason)
        assert res.state == "failed"
        assert res.is_verified is False


# ============================================================================
# 4. Refusal cannot produce successful completion
# ============================================================================

def test_refusal_cannot_produce_successful_completion():
    """A refusal, capability denial, or explanation must NEVER produce a completed state."""
    refusal_messages = [
        "I cannot access browsers, external apps or email accounts.",
        "As an AI language model, I do not have access to the internet.",
        "Sorry, but I cannot interact with external services.",
        "I am unable to browse websites or open mail.",
        # Regression: "I don't have the details of the previous/unfinished
        # task…" used to pass the refusal filter as CONVERSATIONAL and was
        # verified as a completed mission. It is a denial, not a result.
        "I don't have the details of the previous or unfinished task in our current context. Could you please specify what task you'd like me to complete?",
        "I do not have information about that request.",
        "I lack context regarding the task you mentioned.",
    ]
    for ref in refusal_messages:
        assert is_refusal_message(ref) is True, f"'{ref}' must be detected as refusal"
        outcome = verify_task_completion("open mail", "completed", ref)
        assert outcome.state == "failed", "Refusal must never be marked completed"
        assert outcome.is_verified is False
        assert "refusal" in outcome.reason.lower()


# ============================================================================
# 5. Duplicate mission results are prevented
# ============================================================================

@pytest.mark.asyncio
async def test_duplicate_mission_results_are_prevented():
    """Hub suppresses duplicate terminal progress broadcasts for the same task."""
    test_hub = Hub()
    task = TaskRecord(task_id="dup-test-task", instruction="open mail", state="running")
    test_hub.tasks[task.task_id] = task

    sent_payloads = []

    class MockWebSocket:
        async def send_json(self, data: Any) -> None:
            sent_payloads.append(data)

    mock_ws = MockWebSocket()
    test_hub.client_sockets.append(mock_ws)

    # 1. Non-terminal progress
    task.state = "running"
    task.message = "Navigating to Gmail..."
    await test_hub.broadcast_progress(task)
    assert len(sent_payloads) == 1

    # 2. First terminal progress
    task.state = "completed"
    task.message = "Successfully opened Gmail."
    await test_hub.broadcast_progress(task)
    assert len(sent_payloads) == 2
    assert sent_payloads[-1]["payload"]["taskState"] == "completed"

    # 3. Duplicate terminal broadcast attempt (e.g. from polling or late callback)
    await test_hub.broadcast_progress(task)
    # Must remain 2: duplicate terminal broadcast suppressed!
    assert len(sent_payloads) == 2, "Duplicate terminal broadcast must be suppressed"


# ============================================================================
# 6. Multi-site tasks resume without repeating completed actions
# ============================================================================

def test_multi_site_tasks_resume_without_repeating_completed_actions():
    """When resuming a multi-site task, verified sites must not be re-visited."""
    instruction = "Find Samsung Galaxy A27 on Amazon, Flipkart and Samsung's official store and compare prices"
    tracker = init_multi_site_tracker(instruction)
    assert tracker is not None

    # Simulate Amazon was already completed and extracted
    tracker = update_multi_site_tracker(
        tracker,
        url="https://www.amazon.in/dp/B0XYZ123",
        title="Samsung Galaxy A27 - Amazon.in",
        page_text="Price: ₹30,700",
        collected_research={"amazon.in": {"price": "₹30,700", "url": "https://www.amazon.in/dp/B0XYZ123"}},
    )
    assert tracker["required_sites"]["amazon"]["status"] == "completed"

    # Verify Amazon is marked as already resolved
    assert is_already_resolved_site("https://www.amazon.in/s?k=samsung", tracker) is True

    # Check pending sites: only Flipkart and Samsung Store should remain
    pending = get_pending_sites(tracker)
    pending_names = [s["name"] for s in pending]
    assert "Amazon" not in pending_names
    assert "Flipkart" in pending_names
    assert "Samsung Store" in pending_names

    # If currently on Flipkart, planner must NOT navigate to Flipkart default_url again
    flipkart_payload = _make_payload(
        url="https://www.flipkart.com/",
        title="Online Shopping Site for Mobiles, Electronics",
        interactive=[
            {"targetId": "fk_search", "role": "combobox", "name": "Search for Products, Brands and More", "tag": "input"},
        ],
        instruction=instruction,
    )
    class _MockEmptyPlanner:
        def plan_call(self, instruction, payload, history, working_memory=None):
            return {"goal": "Compare", "steps": [], "answer": None}

    guarded_plan = plan_from_observation(
        instruction=instruction,
        payload=flipkart_payload,
        provider=_MockEmptyPlanner(),
        history=[{"action": {"type": "navigate", "params": {"url": "https://www.flipkart.com/"}}, "result_ok": True}],
        working_memory={"multi_site_research": tracker},
    )
    # The guarded plan must search on Flipkart instead of navigating to flipkart.com again
    assert len(guarded_plan.steps) >= 1
    step_action = guarded_plan.steps[0].action
    assert step_action.type == "type", "When on Flipkart, must type search query rather than re-navigating"
    assert "Samsung Galaxy A27" in step_action.params.text


# ============================================================================
# 7. Provider failure cannot produce false completion
# ============================================================================

def test_provider_failure_cannot_produce_false_completion():
    """Provider error messages, timeouts, and quota exhaustion must never be marked completed."""
    provider_errors = [
        "Planner unavailable: 503 UNAVAILABLE: model service temporarily down",
        "Planner timed out: model provider did not respond within 45s deadline",
        "Planner unavailable: 429 RESOURCE_EXHAUSTED: quota exceeded, retry in 1m",
    ]
    for err in provider_errors:
        res = verify_task_completion("Find Samsung Galaxy A27 on Amazon and Flipkart", "completed", err)
        assert res.state == "failed", f"'{err}' must be marked as failed"
        assert res.is_verified is False
        assert "error" in res.reason.lower() or "timeout" in res.reason.lower()


# ============================================================================
# 8. Follow-up email context retention & session continuation
# ============================================================================

def test_email_followup_continues_existing_browser_session():
    """Follow-up questions like 'Is there any other mail more important than this?' must continue the session."""
    from app.session_manager import session_manager
    session_manager.clear()

    sess = session_manager.create_session("chat-email-1", "task-mail-init")
    session_manager.pin_primary_tab(sess.session_id, 101)

    follow_up = "Is there any other mail more important than this?"
    classification = classify_intent(follow_up, context={"active_session": sess})
    assert classification.is_browser is True
    assert classification.intent == TaskIntent.CONTINUE_BROWSER_TASK

    # Verify session continuation rebinds existing session and preserves primary tab #101
    cont_sess = session_manager.continue_session(sess.session_id, "task-mail-followup")
    assert cont_sess is not None
    assert cont_sess.session_id == sess.session_id
    assert cont_sess.primary_tab_id == 101


# ============================================================================
# 9. Task recovery from checkpoint without 404
# ============================================================================

def test_task_checkpoint_restored_from_db_without_404():
    """Tasks missing from in-memory registry must be restored from SQLite checkpoints."""
    from app import db
    from app.main import _require

    tid = "test-pruned-task-xyz"
    db.save_task_checkpoint(
        task_id=tid,
        conversation_id="conv-123",
        session_id="sess-123",
        step_index=3,
        last_url="https://mail.google.com/",
        history=[{"action": {"type": "navigate"}, "result_ok": True}],
        working_memory={"instruction": "open mail", "notes": "Email analyzed."},
    )

    # Ensure task is not in memory
    from app.hub import hub
    hub.tasks.pop(tid, None)

    # _require must restore it without raising HTTPException(404)
    restored = _require(tid)
    assert restored is not None
    assert restored.task_id == tid
    assert restored.chat_id == "conv-123"
    assert restored.steps_done == 3
    assert restored.session_id == "sess-123"

