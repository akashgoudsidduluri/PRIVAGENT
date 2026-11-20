"""Regression tests for the 2026-11-20 live-test fixes.

Fix 1 (frontend, see tests/timeline_clear.test.mjs): + New Mission
clears the TASK TIMELINE panel.
Fix 2: /api/connectors status is fast (unconfigured providers skip
live verification) — see test_oauth_status_* below.
Fix 3 (calendar routing / status checks / multi-turn follow-ups /
multi-event reminders) was REVERTED 2026-11-20 on the user's call —
the calendar part was not good — so its tests are gone with it.
Fix 4: <function=...> tags stripped from conversational output.

Every test here is written against the production failure first:
the live report was <function=calltool> leaking into chat replies.
"""
import time

import httpx

from app.connectors.oauth import OAuthManager
from app.loop_helpers import strip_tool_call_blocks


# ---------------------------------------------------------------------------
# Fix 4 — <function=...> tags stripped
# ---------------------------------------------------------------------------

def test_function_tags_stripped_from_conversational_output():
    answer, names = strip_tool_call_blocks(
        'Here is the plan <function=calltool>{"name": "browse"}</function> done.'
    )
    assert "<function" not in answer
    assert "calltool" in names or "browse" in names
    assert "Here is the plan" in answer and "done." in answer

    answer, _ = strip_tool_call_blocks("Quick check <function=search/> then answer.")
    assert "<function" not in answer
    assert "Quick check" in answer and "then answer." in answer

    # Truncated (unclosed) block is cut, like <tool_call> is.
    answer, _ = strip_tool_call_blocks("Answer starts here <function=browse>{\"name\": \"browse\"}")
    assert "<function" not in answer
    assert answer.strip() == "Answer starts here"

    # Plain text without tags is untouched.
    answer, names = strip_tool_call_blocks("Just a normal answer.")
    assert answer == "Just a normal answer."
    assert names == []


# ---------------------------------------------------------------------------
# Fix 2 — /api/connectors status is fast
# ---------------------------------------------------------------------------

def test_oauth_status_skips_unconfigured_providers_fast():
    def _no_network(request):
        raise AssertionError(f"unexpected live call to {request.url}")

    manager = OAuthManager(client=httpx.Client(transport=httpx.MockTransport(_no_network)))
    started = time.monotonic()
    results = manager.status()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert len(results) == 4
    assert all(r["connected"] is False for r in results)
    assert all(r["configured"] is False for r in results)
