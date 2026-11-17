"""Task 4 (latency): STALE_TARGET short-circuit + first-step warm-up.

Covers the two measured wins of this task, at the level where each
is decided:

* ``_execute_with_retries`` returns immediately on STALE_TARGET for
  non-idempotent actions (click / type) — one dispatch, no backoff
  sleep — while idempotent actions (scroll) still retry and other
  recoverable errors on a click still retry. The loop's fresh-ref
  recovery is unchanged (see test_automation.py, which pins exactly
  two dispatches for a recovered stale click).
* ``warm_loop_caches`` derives the Fernet key and imports the
  redaction modules lazily loaded on first use, so the first step of
  the first task in a process no longer pays ~200 ms of one-time
  costs inside its redact / checkpoint phases.
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from app import agentLoop
from app import secrets as secrets_store
from app.loop_helpers import _execute_with_retries, warm_loop_caches


class _CountingHub:
    """Returns one canned failure for every dispatch; counts them."""

    def __init__(self, error: dict[str, Any]) -> None:
        self.error = error
        self.calls = 0

    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.calls += 1
        return {"ok": False, "error": dict(self.error)}


_STALE = {"code": "STALE_TARGET", "message": "gone", "recoverable": True}


def test_stale_target_returns_immediately_for_non_idempotent_action():
    hub = _CountingHub(_STALE)
    started = time.perf_counter()
    res = asyncio.run(_execute_with_retries(
        hub, {"type": "click", "timeoutMs": 1000}, "t",
        max_retries=agentLoop.MAX_RETRIES,
    ))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert res["error"]["code"] == "STALE_TARGET"
    # One dispatch, not 1 + MAX_RETRIES. The old code slept
    # 0.2 + 0.4 + 0.6 = 1.2 s of backoff across its retries; the
    # short-circuit must return well inside that.
    assert hub.calls == 1
    assert elapsed_ms < 1000.0


def test_stale_target_returns_immediately_for_type_action():
    hub = _CountingHub(_STALE)
    res = asyncio.run(_execute_with_retries(
        hub, {"type": "type", "timeoutMs": 1000,
              "target": {"targetId": "password", "elementRef": "e1"}},
        "t", max_retries=agentLoop.MAX_RETRIES,
    ))
    assert res["error"]["code"] == "STALE_TARGET"
    assert hub.calls == 1


def test_stale_target_still_retried_for_idempotent_action():
    # Idempotent actions carry no element target: a stale report is
    # treated as transient, exactly like the pre-existing TIMEOUT rule.
    hub = _CountingHub(_STALE)
    res = asyncio.run(_execute_with_retries(
        hub, {"type": "scroll", "timeoutMs": 1000}, "t",
        max_retries=agentLoop.MAX_RETRIES,
    ))
    assert res["error"]["code"] == "STALE_TARGET"
    assert hub.calls == agentLoop.MAX_RETRIES + 1


def test_other_recoverable_errors_still_retried_for_click():
    # The fix is STALE-specific: a genuinely transient failure on a
    # click still gets its bounded retries.
    hub = _CountingHub({"code": "EXTENSION_BUSY", "message": "busy",
                        "recoverable": True})
    res = asyncio.run(_execute_with_retries(
        hub, {"type": "click", "timeoutMs": 1000}, "t",
        max_retries=agentLoop.MAX_RETRIES,
    ))
    assert res["error"]["code"] == "EXTENSION_BUSY"
    assert hub.calls == agentLoop.MAX_RETRIES + 1


def test_non_recoverable_stale_target_returns_immediately():
    hub = _CountingHub({"code": "STALE_TARGET", "message": "gone",
                        "recoverable": False})
    res = asyncio.run(_execute_with_retries(
        hub, {"type": "click", "timeoutMs": 1000}, "t",
        max_retries=agentLoop.MAX_RETRIES,
    ))
    assert hub.calls == 1


def test_warm_loop_caches_derives_key_and_imports_redaction_modules():
    # Evict the warmed state first, so the test proves the warm-up
    # (re)creates it instead of passing because an earlier test did.
    secrets_store._fernet_cache.pop("fernet", None)
    for mod in ("app.grounding", "app.vault", "app.adversarial_guard",
                "app.webmcp"):
        sys.modules.pop(mod, None)
    warm_loop_caches()
    assert "fernet" in secrets_store._fernet_cache
    for mod in ("app.grounding", "app.vault", "app.adversarial_guard",
                "app.webmcp"):
        assert mod in sys.modules
    # Second call is a no-op, not a second derivation.
    warm_loop_caches()
    assert "fernet" in secrets_store._fernet_cache


def test_warm_loop_caches_never_raises(monkeypatch):
    # A warm-up failure must never break a task: the first step just
    # pays the one-time cost as before.
    monkeypatch.setattr(secrets_store, "warm_key",
                        lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    warm_loop_caches()


def _obs() -> dict[str, Any]:
    return {
        "tabId": 1, "url": "https://example.org/", "title": "Home",
        "interactive": [{
            "targetId": "go", "elementRef": "e0", "role": "link",
            "name": "Go onward", "tag": "a", "visible": True,
            "enabled": True,
            "bounds": {"x": 100, "y": 100, "width": 200, "height": 30},
        }],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1,
    }


class _OkHub:
    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        return {"ok": True, "evidence": "ok", "observation": _obs()}


def test_first_step_phases_are_small_after_a_cold_key():
    """With the key cache cleared, the first step must still be fast.

    Pre-fix this failed: the first checkpoint derived the Fernet key
    (~140-175 ms of PBKDF2) inside the step, so step 1 checkpoint_ms
    was ~165-246 ms while later steps were ~3 ms. _run_loop warms the
    key before the first step, so the derivation happens outside every
    measured phase. The key state is saved and restored so no other
    test in this process sees the reset.
    """
    saved_cache = dict(secrets_store._fernet_cache)
    saved_material = secrets_store._ephemeral_material
    try:
        secrets_store.reset_for_tests()
        calls = {"n": 0}

        def planner(instruction, payload, history, working_memory=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "g", "steps": [{"why": "w", "action": {
                    "type": "click", "risk": "write", "verify": "clicked",
                    "timeoutMs": 5000,
                    "target": {"targetId": "go", "name": "Go onward",
                               "elementRef": "e0"},
                }}]}
            return {"goal": "g", "steps": []}

        result = asyncio.run(agentLoop.run_with_injection(
            _OkHub(), planner=planner, obs_factory=_obs,
            task_id="latency-warmup-test", instruction="go onward",
        ))
        assert result["final"] == "completed"
        first = result["timings"]["steps"][0]
        assert first["checkpoint_ms"] < 50
        assert first["redact_ms"] < 50
    finally:
        secrets_store._fernet_cache.clear()
        secrets_store._fernet_cache.update(saved_cache)
        secrets_store._ephemeral_material = saved_material
