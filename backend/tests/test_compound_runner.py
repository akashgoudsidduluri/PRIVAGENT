"""Compound-instruction runner: sequential sub-tasks with threading.

Live 2026-11-19: "open google, research about black holes and send
it to prateek in whatsapp, also ask a random question to him, and
then open amazon and send redmi phone link to prateek" ran 25 steps,
delivered nothing, and reported the research as incomplete while the
WhatsApp/Amazon halves vanished. The runner must split the instruction
into parts, run each as its own mission in order, thread earlier
results into later parts, and report every part's outcome honestly —
never silently "completed" with parts undone.
"""
from __future__ import annotations

import asyncio

from app import agentLoop, db
from app.hub import TaskRecord


class _MockHub:
    extension_sockets = ["dummy_ws"]

    def __init__(self):
        self.tasks: dict[str, TaskRecord] = {}
        self.dispatched: list[dict] = []

    def spawn(self, coro):
        return asyncio.create_task(coro)

    async def broadcast_progress(self, record):
        pass

    async def send_action(self, task_id, action, timeout_s=45.0):
        self.dispatched.append(action)
        return {"ok": True}


def _run_compound(monkeypatch, instruction: str, task_id: str = "task_compound_01"):
    mock_hub = _MockHub()
    monkeypatch.setattr(agentLoop, "real_hub", mock_hub)
    chat = db.create_chat("Compound runner test")
    record = TaskRecord(task_id=task_id, instruction=instruction, chat_id=chat["id"])
    mock_hub.tasks[task_id] = record
    asyncio.run(agentLoop.run_task(task_id))
    return mock_hub, chat["id"], record


def test_compound_all_parts_complete(monkeypatch):
    """Two explainable parts complete; the chat keeps ONE user message."""
    mock_hub, chat_id, record = _run_compound(
        monkeypatch, "explain black holes and explain quantum entanglement"
    )
    assert record.intent == "COMPOUND"
    assert record.state == "completed"
    assert "Done in 2 parts" in record.message
    assert "Part 1 (explain black holes): completed" in record.message
    assert "Part 2 (explain quantum entanglement): completed" in record.message
    assert "all parts completed" in record.message
    # Both sub-tasks ran as their own missions under the parent.
    subs = [t for t in mock_hub.tasks.values() if t.compound_part]
    assert len(subs) == 2
    assert all(t.state == "completed" for t in subs)
    assert all(t.parent_task_id == record.task_id for t in subs)
    # The user sent ONE message; the parts never added their own.
    messages = db.get_messages(chat_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert "explain black holes and explain quantum entanglement" in user_msgs[0]["content"]
    agent_msgs = [m for m in messages if m["role"] == "agent"]
    assert len(agent_msgs) >= 3  # one per part + the summary


def test_compound_partial_failure_is_honest_not_completed(monkeypatch):
    """A part that fails honestly fails the mission.

    Part 1 completes; part 2 ("open my inbox") is a browser task that
    fails honestly in this sandbox (no live extension to observe with).
    The mission must be "failed" with both parts named — never
    "completed" with part 2 silently dropped, as the live compound
    run did.
    """
    mock_hub, chat_id, record = _run_compound(
        monkeypatch, "explain black holes and open my inbox"
    )
    assert record.state == "failed"
    assert "Part 1 (explain black holes): completed" in record.message
    assert "Part 2 (open my inbox): failed" in record.message
    assert "all parts completed" not in record.message


def test_compound_dependent_part_skipped_when_dependency_failed(monkeypatch):
    """A part whose "it" has no summary is skipped, never sent blind.

    Part 1 ("send the summary to prateek on telegram") fails — in this
    sandbox the browser task cannot observe — so there is no summary
    for part 2 ("send it to prateek in whatsapp") to send. The runner
    skips part 2 with an honest reason instead of running a WhatsApp
    send with an unresolved "it" — and no send action ever reaches
    the extension (only the failed part's observe attempts).
    """
    mock_hub, chat_id, record = _run_compound(
        monkeypatch, "send the summary to prateek on telegram and send it to prateek in whatsapp"
    )
    assert record.state == "failed"
    assert "Part 1 (send the summary to prateek on telegram): failed" in record.message
    assert "Part 2 (send it to prateek in whatsapp): skipped" in record.message
    assert "depends on an earlier part that failed" in record.message
    assert mock_hub.dispatched, "part 1 must have attempted the browser"
    # Only read-only/setup actions reached the extension (session init
    # + observe attempts) — no send, click, or navigate ever ran.
    assert all(a.get("type") in ("init_session", "observe") for a in mock_hub.dispatched)
    assert not any(a.get("type") in ("type", "click", "navigate") for a in mock_hub.dispatched)


def test_exact_live_compound_routes_every_part_correctly(monkeypatch):
    """Review #4, S6/M1: the exact live compound through run_task.

    Pre-fix, the threaded WhatsApp send misrouted to a garbage research
    run and the question part "completed" as a chat greeting without
    ever asking Prateek. Post-fix every part must carry the right
    intent and the right (threaded) instruction. In this sandbox there
    is no live extension, so the browser parts fail honestly with
    "observation failed" — the assertions are about routing and
    threading, not delivery; delivery needs the user's live Chrome.
    """
    instruction = (
        "open google, research about black holes and send it to prateek in "
        "whatsapp, also ask a random question to him, and then open amazon "
        "and send redmi phone link to prateek"
    )
    mock_hub, chat_id, record = _run_compound(monkeypatch, instruction, task_id="task_exact_live")
    assert record.intent == "COMPOUND"
    assert record.state == "failed"  # sandbox: no extension to observe with
    assert "all parts completed" not in record.message
    subs = {t.task_id: t for t in mock_hub.tasks.values() if t.compound_part}
    assert len(subs) == 3  # part 2 was skipped, never created a sub-task
    part1 = subs["task_exact_live__part1"]
    assert part1.instruction == "open google, research about black holes"
    assert part1.intent == "RESEARCH_TASK"
    # Part 2 never ran: no summary existed (part 1 failed), so it was
    # skipped honestly instead of sending garbage.
    assert "Part 2 (send it to prateek in whatsapp): skipped" in record.message
    assert "depends on an earlier part that failed" in record.message
    # Part 3 inherited the recipient AND the channel from the skipped
    # part 2's text, and routes to the browser — never a chat greeting.
    part3 = subs["task_exact_live__part3"]
    assert part3.instruction == "ask a random question to Prateek on whatsapp"
    assert part3.intent == "NEW_BROWSER_TASK"
    assert "Hello! I am PrivAgent" not in (part3.message or "")
    # Part 4 inherited the channel too and routes to the browser.
    part4 = subs["task_exact_live__part4"]
    assert part4.instruction == "open amazon, send redmi phone link to prateek on whatsapp"
    assert part4.intent == "NEW_BROWSER_TASK"
