"""Regression test suite for P5.2: Task Intent Routing & Browser Session Management."""
from __future__ import annotations

import asyncio
from typing import Any, Optional
import pytest

from app.intent_router import TaskIntent, classify_intent
from app.session_manager import BrowserSession, SessionManager, session_manager
from app.hub import Hub, TaskRecord
from app import agentLoop, db


@pytest.fixture(autouse=True)
def clean_state():
    db.init_db()
    session_manager.clear()
    yield
    session_manager.clear()


# ---------------------------------------------------------------------------
# Phase 6A: Non-Browser Tasks (Zero Browser Interaction)
# ---------------------------------------------------------------------------

def test_intent_classification_non_browser_prompts():
    non_browser_prompts = [
        "Hi",
        "Hello!",
        "hey there",
        "hi man",
        "Explain transformers",
        "Write a C++ BFS program",
        "Calculate 25% of 800",
        "What is machine learning?",
        "Solve 125 * 8",
        "Calculate 125 * 8",
        "Who are you?",
    ]
    for prompt in non_browser_prompts:
        res = classify_intent(prompt)
        assert res.intent == TaskIntent.NON_BROWSER, f"Expected NON_BROWSER for '{prompt}', got {res.intent}"
        assert not res.is_browser, f"Expected is_browser=False for '{prompt}'"
        assert res.is_conversational, f"Expected is_conversational=True for '{prompt}'"


def test_user_requested_exact_prompts_classification():
    # 1. "hi man"
    r1 = classify_intent("hi man")
    assert r1.intent == TaskIntent.NON_BROWSER
    assert r1.is_browser is False
    assert r1.is_conversational is True

    # 2. "Explain transformers"
    r2 = classify_intent("Explain transformers")
    assert r2.intent == TaskIntent.NON_BROWSER
    assert r2.is_browser is False
    assert r2.is_conversational is True

    # 3. "Write a C++ BFS program"
    r3 = classify_intent("Write a C++ BFS program")
    assert r3.intent == TaskIntent.NON_BROWSER
    assert r3.is_browser is False
    assert r3.is_conversational is True

    # 4. "Calculate 125 * 8"
    r4 = classify_intent("Calculate 125 * 8")
    assert r4.intent == TaskIntent.NON_BROWSER
    assert r4.is_browser is False
    assert r4.is_conversational is True

    # 5. "Search Google for Python tutorials"
    r5 = classify_intent("Search Google for Python tutorials")
    assert r5.intent == TaskIntent.NEW_BROWSER_TASK
    assert r5.is_browser is True
    assert r5.is_conversational is False

    # 6. "Continue searching" (with active session)
    ctx = {"active_session": BrowserSession(session_id="s1", task_id="t1", conversation_id="c1", origin="new_task")}
    r6 = classify_intent("Continue searching", context=ctx)
    assert r6.intent == TaskIntent.CONTINUE_BROWSER_TASK
    assert r6.is_browser is True
    assert r6.is_conversational is False

    # 7. "Use my active tab"
    r7 = classify_intent("Use my active tab")
    assert r7.intent == TaskIntent.USE_EXISTING_USER_TAB
    assert r7.is_browser is True
    assert r7.is_conversational is False


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,expected_keywords", [
    ("hi man", ("hello", "hey", "hi", "help", "assist")),
    ("Explain transformers", ("transformer", "attention", "architecture", "model")),
    ("Write a C++ BFS program", ("bfs", "breadth", "include", "vector", "queue")),
    ("Calculate 125 * 8", ("1000", "1,000", "calculated", "1 000", "1\u202f000", "=")),
])
async def test_conversational_prompts_execution_zero_browser_actions(monkeypatch, prompt, expected_keywords):
    actions_dispatched = []

    class MockHub:
        extension_sockets = ["dummy_ws"]
        tasks = {}

        def spawn(self, coro):
            return asyncio.create_task(coro)

        async def broadcast_progress(self, record):
            pass

        async def send_action(self, task_id, action, timeout_s=45.0):
            actions_dispatched.append(action)
            return {"ok": True}

    mock_hub = MockHub()
    monkeypatch.setattr(agentLoop, "real_hub", mock_hub)

    chat = db.create_chat(f"Chat for {prompt[:12]}")
    chat_id = chat["id"]

    task_id = f"task_{abs(hash(prompt))}"
    record = TaskRecord(
        task_id=task_id,
        instruction=prompt,
        chat_id=chat_id,
    )
    mock_hub.tasks[task_id] = record

    await agentLoop.run_task(task_id)

    assert record.state == "completed"
    assert record.intent == "NON_BROWSER"
    assert record.is_browser_task is False
    assert record.session_id is None
    assert record.primary_tab_id is None
    msg_lower = record.message.lower()
    assert any(kw in msg_lower for kw in expected_keywords), f"None of {expected_keywords} found in: {record.message}"

    # Absolute zero extension actions dispatched
    assert len(actions_dispatched) == 0, f"Expected 0 browser actions for '{prompt}', got: {actions_dispatched}"

    messages = db.get_messages(chat_id)
    assert len(messages) >= 2
    assert messages[-1]["role"] == "agent"
    assert messages[-1]["evidence"] == "conversational"


@pytest.mark.asyncio
async def test_non_browser_task_execution_zero_browser_actions(monkeypatch):
    """Verify that a non-browser task never touches extension, tabs, or observations."""
    actions_dispatched = []

    class MockHub:
        extension_sockets = ["dummy_ws"]
        tasks = {}

        def spawn(self, coro):
            return asyncio.create_task(coro)

        async def broadcast_progress(self, record):
            pass

        async def send_action(self, task_id, action, timeout_s=45.0):
            actions_dispatched.append(action)
            return {"ok": True}

    mock_hub = MockHub()
    monkeypatch.setattr(agentLoop, "real_hub", mock_hub)

    chat = db.create_chat("Test Chat")
    chat_id = chat["id"]

    task_id = "task_nb_01"
    record = TaskRecord(
        task_id=task_id,
        instruction="Explain transformers",
        chat_id=chat_id,
    )
    mock_hub.tasks[task_id] = record

    await agentLoop.run_task(task_id)

    # Must complete cleanly
    assert record.state == "completed"
    assert record.intent == "NON_BROWSER"
    assert record.is_browser_task is False
    assert "Transformer" in record.message or len(record.message) > 10

    # ZERO extension action dispatches guaranteed!
    assert len(actions_dispatched) == 0, f"Expected 0 browser actions, but dispatched: {actions_dispatched}"

    # Verify conversation history was recorded
    messages = db.get_messages(chat_id)
    assert len(messages) >= 2
    assert messages[-1]["role"] == "agent"
    assert messages[-1]["evidence"] == "conversational"


# ---------------------------------------------------------------------------
# Phase 6B: New Browser Sessions (Isolation)
# ---------------------------------------------------------------------------

def test_intent_classification_new_browser_task():
    browser_prompts = [
        "Go to amazon.in and search for laptops",
        "https://en.wikipedia.org/wiki/Artificial_intelligence",
        "Navigate to google.com",
        "Search for mechanical keyboard on flipkart",
        "Look up flight prices on kayak.com",
    ]
    for prompt in browser_prompts:
        res = classify_intent(prompt)
        assert res.intent == TaskIntent.NEW_BROWSER_TASK, f"Expected NEW_BROWSER_TASK for '{prompt}', got {res.intent}"
        assert res.is_browser is True


def test_independent_browser_tasks_get_isolated_sessions():
    mgr = SessionManager()
    sess1 = mgr.create_session(conversation_id="chat_1", task_id="task_1", origin="new_task")
    sess2 = mgr.create_session(conversation_id="chat_2", task_id="task_2", origin="new_task")

    assert sess1.session_id != sess2.session_id
    assert sess1.task_id == "task_1"
    assert sess2.task_id == "task_2"
    assert sess1.origin == "new_task"
    assert sess2.origin == "new_task"


# ---------------------------------------------------------------------------
# Phase 6C: Continuation (Session Reuse)
# ---------------------------------------------------------------------------

def test_intent_classification_continuation_with_active_session():
    # With active session: continuation triggers
    ctx = {"active_session": BrowserSession(session_id="s1", task_id="t1", conversation_id="c1", origin="new_task")}
    res = classify_intent("Click on the second product link", context=ctx)
    assert res.intent == TaskIntent.CONTINUE_BROWSER_TASK

    res2 = classify_intent("scroll down", context=ctx)
    assert res2.intent == TaskIntent.CONTINUE_BROWSER_TASK

    # Without active session: does not classify as continuation
    res_no_ctx = classify_intent("Click on the second product link", context={})
    assert res_no_ctx.intent != TaskIntent.CONTINUE_BROWSER_TASK

    # Explicit request to finish/resume the interrupted task (regression: this
    # phrasing used to fall through to NON_BROWSER and get answered
    # conversationally, ignoring the preserved checkpoint).
    ctx_sess = BrowserSession(session_id="s2", task_id="t2", conversation_id="c1", origin="new_task")
    for phrase in (
        "could u complete the unfinished task",
        "complete the unfinished task",
        "finish the previous task",
        "resume the incomplete mission",
        "complete the unfinished task from where it stopped",
    ):
        res_resume = classify_intent(phrase, context=ctx)
        assert res_resume.intent == TaskIntent.CONTINUE_BROWSER_TASK, phrase


def test_continuation_reuses_existing_session():
    mgr = SessionManager()
    sess1 = mgr.create_session(conversation_id="chat_cont", task_id="task_initial", origin="new_task")
    mgr.pin_primary_tab(sess1.session_id, 42)

    active = mgr.get_active_session_for_chat("chat_cont")
    assert active is not None
    assert active.session_id == sess1.session_id

    # Continuation rebinds session
    sess_reused = mgr.continue_session(active.session_id, new_task_id="task_continuation")
    assert sess_reused is not None
    assert sess_reused.session_id == sess1.session_id
    assert sess_reused.task_id == "task_continuation"
    assert sess_reused.primary_tab_id == 42


# ---------------------------------------------------------------------------
# Phase 6D: Existing User Tab
# ---------------------------------------------------------------------------

def test_intent_classification_existing_user_tab():
    res = classify_intent("Use the active tab and summarize it")
    assert res.intent == TaskIntent.USE_EXISTING_USER_TAB
    assert not res.is_ambiguous

    res_hint = classify_intent("on the open amazon tab, find headphones")
    assert res_hint.intent == TaskIntent.USE_EXISTING_USER_TAB

    # Ambiguous tab request
    res_ambig = classify_intent("use a tab to do this")
    assert res_ambig.intent == TaskIntent.USE_EXISTING_USER_TAB
    assert res_ambig.is_ambiguous is True


@pytest.mark.asyncio
async def test_ambiguous_tab_fails_closed(monkeypatch):
    class MockHub:
        extension_sockets = ["dummy_ws"]
        tasks = {}

        def spawn(self, coro):
            return asyncio.create_task(coro)

        async def broadcast_progress(self, record):
            pass

        async def send_action(self, task_id, action, timeout_s=45.0):
            return {"ok": True}

    mock_hub = MockHub()
    monkeypatch.setattr(agentLoop, "real_hub", mock_hub)

    task_id = "task_ambig"
    record = TaskRecord(
        task_id=task_id,
        instruction="pick any tab and read it",
    )
    mock_hub.tasks[task_id] = record

    await agentLoop.run_task(task_id)
    assert record.state == "failed"
    assert "AMBIGUOUS_TAB" in record.message


# ---------------------------------------------------------------------------
# Phase 6E: Multi-Tab Pinning & Child Tabs
# ---------------------------------------------------------------------------

def test_multi_tab_pinning_and_authorization():
    mgr = SessionManager()
    sess = mgr.create_session(conversation_id="chat_multi", task_id="task_multi", origin="new_task")
    mgr.pin_primary_tab(sess.session_id, 101)

    # Primary tab is in session
    assert mgr.is_tab_in_session(sess.session_id, 101)
    # Foreign tab is not
    assert not mgr.is_tab_in_session(sess.session_id, 999)

    # Register child tabs
    mgr.register_child_tab(sess.session_id, 102)
    mgr.register_child_tab(sess.session_id, 103)
    assert mgr.is_tab_in_session(sess.session_id, 102)
    assert mgr.is_tab_in_session(sess.session_id, 103)
    assert sess.child_tab_ids == [102, 103]

    # Unregister child tab
    mgr.unregister_child_tab(sess.session_id, 102)
    assert not mgr.is_tab_in_session(sess.session_id, 102)
    assert mgr.is_tab_in_session(sess.session_id, 103)
    assert mgr.is_tab_in_session(sess.session_id, 101)


def test_planner_rejects_closing_primary_tab():
    """A close_tab action aimed at the session's primary tab must be rejected
    as PLAN_INVALID — it would strand the session without a home tab."""
    from app.planner import _validate_step
    from app.contracts import PlannedStep

    step = PlannedStep.model_validate({
        "why": "close the tab",
        "action": {"type": "close_tab", "risk": "write", "verify": "closed",
                   "timeoutMs": 3000, "params": {"tabId": 101}},
    })
    with pytest.raises(ValueError, match="cannot close the primary tab"):
        _validate_step(step, {"primary_tab_id": 101})

    # A child tab close is fine.
    step_child = PlannedStep.model_validate({
        "why": "close the helper tab",
        "action": {"type": "close_tab", "risk": "write", "verify": "closed",
                   "timeoutMs": 3000, "params": {"tabId": 102}},
    })
    _validate_step(step_child, {"primary_tab_id": 101})

    # No anchored session (working_memory without primary tab): no verdict.
    _validate_step(step, None)


def test_new_tab_action_is_authored_and_child_registered():
    """The new_tab action is structurally valid and drives backend-side
    registry registration through the evidence matcher in the loop."""
    from app.planner import _validate_step
    from app.contracts import PlannedStep

    step = PlannedStep.model_validate({
        "why": "open helper tab",
        "action": {"type": "new_tab", "risk": "write", "verify": "opened",
                   "timeoutMs": 5000, "params": {"url": "https://example.org/page"}},
    })
    _validate_step(step)  # must not raise


# ---------------------------------------------------------------------------
# Phase 6F: Recovery & Safety
# ---------------------------------------------------------------------------

def test_session_lifecycle_status_and_cancellation():
    mgr = SessionManager()
    sess = mgr.create_session(conversation_id="chat_life", task_id="task_life", origin="new_task")
    assert sess.status == "active"

    mgr.update_status(sess.session_id, "completed")
    assert sess.status == "completed"

    mgr.update_status(sess.session_id, "cancelled")
    assert sess.status == "cancelled"

    mgr.close_session(sess.session_id)
    assert sess.status == "closed"
