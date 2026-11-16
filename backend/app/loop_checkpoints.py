"""Checkpoints, persistence, and history compaction (Task 13).

Split out of :mod:`app.agentLoop` (which re-exports every name here,
so existing imports keep working). These helpers are the loop's only
writers to the checkpoint / chat-memory tables; all of them honor
zero retention (Task 10) — see :func:`_save_checkpoint_defensive`,
:func:`_persist_agent_message`, :func:`_persist_derived_memory`.
"""
from __future__ import annotations

from typing import Any, Optional

from . import config

__all__ = [
    "_compact_history",
    "_maybe_compact_history",
    "_persist_agent_message",
    "_persist_derived_memory",
    "_register_tab_in_memory",
    "_remove_tab_from_memory",
    "_save_checkpoint_defensive",
    "_set_active_tab_in_memory",
    "save_cancel_checkpoint",
]

COMPACTION_EVERY_STEPS = config.COMPACTION_EVERY_STEPS


# -- Task 9: tab registry helpers -----------------------------------------
# The registry lives in working_memory (so it is checkpointed with the
# task) as a list of {"tab_id", "url", "title", "active"} dicts. The
# loop keeps it in sync after every tab action; switch_tab/select_tab
# may only target a tab in the registry, and an unknown tab is rejected
# before anything is sent to the extension.
def _register_tab_in_memory(working_memory: dict[str, Any], tab_id: int, url: str = "", title: str = "", active: bool = False) -> None:
    tabs = working_memory.setdefault("tabs", [])
    for tab in tabs:
        if tab.get("tab_id") == tab_id:
            tab["url"] = url or tab.get("url", "")
            tab["title"] = title or tab.get("title", "")
            break
    else:
        tabs.append({"tab_id": tab_id, "url": url, "title": title, "active": bool(active)})
    if active:
        _set_active_tab_in_memory(working_memory, tab_id)


def _set_active_tab_in_memory(working_memory: dict[str, Any], tab_id: int) -> bool:
    tabs = working_memory.get("tabs") or []
    found = False
    for tab in tabs:
        tab["active"] = tab.get("tab_id") == tab_id
        found = found or tab.get("tab_id") == tab_id
    return found


def _remove_tab_from_memory(working_memory: dict[str, Any], tab_id: int) -> None:
    tabs = working_memory.get("tabs") or []
    working_memory["tabs"] = [t for t in tabs if t.get("tab_id") != tab_id]
    remaining = working_memory["tabs"]
    if remaining and not any(t.get("active") for t in remaining):
        remaining[0]["active"] = True


# -- Task 9: history compaction -------------------------------------------
# Deterministic, no LLM: the summary is built from the recorded
# evidence of the last successful steps plus the instruction. It is
# written into the checkpoint every COMPACTION_EVERY_STEPS steps on a
# long-horizon task, and the planner then sees the summary + the last
# few steps instead of the full history — the prompt stays bounded
# while the task stays coherent.
def _compact_history(history: list[dict[str, Any]], instruction: str) -> str:
    done: list[str] = []
    for entry in reversed(history):
        if entry.get("result_ok") and entry.get("evidence"):
            action = (entry.get("action") or {}).get("type") or "step"
            done.append(f"{action}: {str(entry['evidence'])[:120]}")
        if len(done) >= 5:
            break
    done.reverse()
    failed = [str(e.get("error") or "")[:120] for e in history if not e.get("result_ok") and e.get("error")]
    parts = [f"Goal: {instruction[:200]}"]
    parts.append("Done so far: " + ("; ".join(done) if done else "nothing yet"))
    if failed:
        parts.append("Known failures (do not repeat): " + "; ".join(failed[-3:]))
    parts.append("Remaining: continue toward the goal; verify each step from a fresh observation.")
    return "\n".join(parts)


def _maybe_compact_history(working_memory: dict[str, Any], history: list[dict[str, Any]], instruction: str, steps_done: int, long_horizon: bool) -> None:
    if not long_horizon or steps_done <= 0 or steps_done % COMPACTION_EVERY_STEPS != 0:
        return
    working_memory["long_horizon_summary"] = _compact_history(history, instruction)
    working_memory["long_horizon_summary_at_step"] = steps_done


def save_cancel_checkpoint(task_id: str, instruction: str, steps_done: int, last_url: str = "") -> bool:
    """Checkpoint a task at cancel time, so a cancelled task can still
    be resumed. Only writes when no checkpoint exists yet — the loop
    already checkpoints after every step, so in practice this fires
    only for tasks cancelled before their first step. Returns True
    when a checkpoint was written."""
    # Task 10: zero retention — a cancelled task leaves no checkpoint
    # behind either; there is nothing to resume from, by design.
    try:
        from .zero_retention import checkpoints_allowed

        if not checkpoints_allowed():
            return False
    except Exception:
        pass
    try:
        from . import db

        db.init_db()  # a fresh operator DB has no tables yet; the
        # checkpoint must still be writable (e.g. cancel before step 1)
        if db.get_task_checkpoint(task_id) is not None:
            return False
        db.save_task_checkpoint(
            task_id=task_id,
            conversation_id="default",
            session_id=None,
            step_index=steps_done,
            last_url=last_url or "",
            history=[],
            working_memory={"instruction": instruction, "cancelled": True, "steps_done": steps_done},
        )
        return True
    except Exception:
        return False


def _save_checkpoint_defensive(
    task_id: str,
    conversation_id: Optional[str],
    session_id: Optional[str],
    steps_done: int,
    raw_url: str,
    history: list[dict[str, Any]],
    working_memory: dict[str, Any],
) -> None:
    # Task 10: zero retention — checkpoints are skipped entirely, so
    # a zero-retention task cannot be resumed after a restart. The
    # in-memory history is still pruned separately (see prune_history).
    try:
        from .zero_retention import checkpoints_allowed

        if not checkpoints_allowed():
            return
    except Exception:
        pass
    try:
        from . import db
        # REVIEW (Task 9 audit): save_task_checkpoint assumes the
        # tables exist (main.py inits them at import), but a loop
        # driving a fresh database — or a DB deleted mid-run — would
        # raise here and this helper would swallow it, silently
        # dropping every checkpoint. Init first; it is idempotent.
        db.init_db()
        db.save_task_checkpoint(
            task_id=task_id,
            conversation_id=conversation_id or "default",
            session_id=session_id,
            step_index=steps_done,
            last_url=raw_url or "",
            history=history,
            working_memory=working_memory,
        )
    except Exception:
        pass


def _persist_agent_message(chat_id: str, content: str, evidence: Optional[str] = None,
                           task_id: Optional[str] = None) -> None:
    """Persist one agent chat message — unless zero retention is on.

    REVIEW (Task 10 audit): run_task persisted the agent's summary
    (which quotes the last evidence, i.e. page text) and the derived
    mission facts into the chat SQLite tables even with
    PRIVAGENT_ZERO_RETENTION=true, because only the checkpoint path
    was gated. Under zero retention the operator still sees the
    message live (record.message + broadcast); only the durable copy
    is skipped. The operator's own user message is persisted by the
    caller directly and is unaffected.
    """
    try:
        from .zero_retention import chat_persistence_allowed

        if not chat_persistence_allowed():
            return
    except Exception:
        pass
    from . import db

    db.add_message(chat_id, "agent", content, evidence=evidence, task_id=task_id)
    db.touch_chat(chat_id)


def _persist_derived_memory(chat_id: str, key: str, value: str) -> None:
    """Persist one derived mission fact — unless zero retention is on.

    Same reason as :func:`_persist_agent_message`: extracted facts
    (``last_result`` = summary[:240], canonical URL, extracted price)
    can quote page text. Instruction-derived research metadata
    (topic/format/type — the operator's own words) is persisted by
    the caller directly and is unaffected.
    """
    try:
        from .zero_retention import chat_persistence_allowed

        if not chat_persistence_allowed():
            return
    except Exception:
        pass
    from . import db

    db.set_chat_memory(chat_id, key, value)
