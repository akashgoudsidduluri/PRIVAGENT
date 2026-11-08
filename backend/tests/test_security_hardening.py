"""Security hardening: origin isolation, cancellation propagation, URL/PII
redaction, screenshot policy, plan bounds, and hub resource cleanup."""
import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import agentLoop, config, privacy
from app.agentLoop import (
    _auto_complete_after_send,
    _is_auth_url,
    _is_messaging_host,
)
from app.contracts import BrowserObservation, PrivacyViolation
from app.hub import Hub
from app.main import _origin_allowed, app
from app.planner import MAX_STEPS_PER_PLAN, _validate_plan
from app.privacy import classify_and_redact


# ---------------------------------------------------------------- origins

def test_origin_allowlist_accepts_local_ui_and_extensions():
    assert _origin_allowed("http://localhost:5173")
    assert _origin_allowed("http://127.0.0.1:8000")
    assert _origin_allowed("chrome-extension://abcdefghijklmnop")
    assert _origin_allowed("moz-extension://abc-123")
    assert _origin_allowed(None)  # curl / tests


def test_origin_allowlist_rejects_remote_and_lookalike_origins():
    assert not _origin_allowed("https://evil.example")
    assert not _origin_allowed("http://localhost.evil.example")
    assert not _origin_allowed("null")
    assert not _origin_allowed("http://localhost:5173.evil.example")
    assert not _origin_allowed("file://")


def test_http_origin_guard_blocks_cross_site_post():
    c = TestClient(app)
    r = c.post(
        "/api/task",
        json={"instruction": "exfiltrate"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    allowed = c.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ok"


def test_websocket_origin_check_is_applied():
    from app.main import _ALLOWED_ORIGIN_RE
    assert _ALLOWED_ORIGIN_RE.fullmatch("chrome-extension://abc")
    assert not _ALLOWED_ORIGIN_RE.fullmatch("https://attacker.example")


# ------------------------------------------------------------ cancellation

def test_cancel_endpoint_marks_loop_for_stop():
    from app.main import cancel_task

    hub = agentLoop.real_hub
    task = hub.new_task("cancel me")
    task.state = "running"
    try:
        result = asyncio.run(cancel_task(task.task_id))
        assert result == {"taskState": "cancelled"}
        assert agentLoop.is_cancelled(task.task_id)
        assert task.cancelled is True
    finally:
        agentLoop._cancelled.discard(task.task_id)
        hub.tasks.pop(task.task_id, None)


def test_cancel_endpoint_does_not_rewrite_terminal_tasks():
    from app.main import cancel_task

    hub = agentLoop.real_hub
    task = hub.new_task("already done")
    task.state = "completed"
    try:
        result = asyncio.run(cancel_task(task.task_id))
        assert result == {"taskState": "completed"}
        assert not agentLoop.is_cancelled(task.task_id)
    finally:
        agentLoop._cancelled.discard(task.task_id)
        hub.tasks.pop(task.task_id, None)


# ---------------------------------------------------------------- privacy

def _obs(**overrides: Any) -> BrowserObservation:
    base = {
        "tabId": 1,
        "url": "https://site.local/search",
        "title": "Search",
        "interactive": [],
        "scroll": {},
        "dpr": 1,
    }
    base.update(overrides)
    return BrowserObservation.model_validate(base)


def test_url_userinfo_and_secret_query_values_are_redacted():
    payload, _ = classify_and_redact(
        _obs(
            url="https://user:hunter2@site.local/path?access_token=abc123&q=jwst#token=zzz",
        ),
        instruction="search",
    )
    assert "hunter2" not in payload.url
    assert "abc123" not in payload.url
    assert "zzz" not in payload.url
    assert "q=jwst" in payload.url
    assert "site.local" in payload.url


def test_url_redaction_preserves_encoding_and_scrubs_hash_routes():
    payload, _ = classify_and_redact(
        _obs(url="https://site.local/s?q=hello%20world#/callback?access_token=abc123"),
        instruction="search",
    )
    # Already-encoded values must not be double-encoded.
    assert "q=hello%20world" in payload.url
    assert "hello%2520world" not in payload.url
    assert "abc123" not in payload.url
    assert "access_token=[REDACTED]" in payload.url


def test_redact_text_urls_scrubs_embedded_tokens():
    text = privacy.redact_text_urls("went to https://site.local/x?token=abc123, done")
    assert "abc123" not in text
    assert text.startswith("went to https://site.local/x?token=[REDACTED]")
    assert text.endswith(", done")


def test_title_pii_patterns_are_redacted():
    payload, _ = classify_and_redact(
        _obs(title="Contact alice@example.com or use sk-live-1234567890abcdef"),
        instruction="read",
    )
    assert "alice@example.com" not in payload.title
    assert "sk-live-1234567890abcdef" not in payload.title
    assert "[REDACTED]" in payload.title


def test_screenshot_is_off_by_default(monkeypatch):
    monkeypatch.setattr(privacy.config, "ALLOW_SCREENSHOTS", False, raising=False)
    payload, _ = classify_and_redact(
        _obs(screenshot="data:image/jpeg;base64,AAAA"),
        instruction="look",
    )
    assert payload.screenshot is None


def test_screenshot_rejects_non_raster_mimes(monkeypatch):
    monkeypatch.setattr(privacy.config, "ALLOW_SCREENSHOTS", True, raising=False)
    payload, _ = classify_and_redact(
        _obs(screenshot="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="),
        instruction="look",
    )
    assert payload.screenshot is None


def test_screenshot_opt_in_still_blocks_sensitive_fields(monkeypatch):
    monkeypatch.setattr(privacy.config, "ALLOW_SCREENSHOTS", True, raising=False)
    plain, _ = classify_and_redact(
        _obs(screenshot="data:image/jpeg;base64,AAAA"),
        instruction="look",
    )
    assert plain.screenshot == "data:image/jpeg;base64,AAAA"

    sensitive = _obs(
        screenshot="data:image/jpeg;base64,AAAA",
        interactive=[{
            "targetId": "pw", "role": "textbox", "name": "Password",
            "tag": "input", "inputType": "password", "value": "hunter2",
            "visible": True, "enabled": True,
        }],
    )
    blocked, _ = classify_and_redact(sensitive, instruction="look")
    assert blocked.screenshot is None


def test_privacy_crash_is_fail_closed(monkeypatch):
    def boom(_el):
        raise RuntimeError("boom")

    monkeypatch.setattr(privacy, "_redact_element", boom)
    with pytest.raises(PrivacyViolation):
        classify_and_redact(
            _obs(interactive=[{
                "targetId": "f", "role": "textbox", "name": "Query",
                "tag": "input", "inputType": "text", "value": "v",
                "visible": True, "enabled": True,
            }]),
            instruction="x",
        )


# ------------------------------------------------------------- auth scope

def test_auth_detection_uses_path_segments_not_substrings():
    assert _is_auth_url("https://example.com/login")
    assert _is_auth_url("https://example.com/account/sign-in")
    assert _is_auth_url("https://example.com/account/verify-email")
    assert _is_auth_url("https://example.com/reset-password")
    assert _is_auth_url("https://accounts.google.com/signin/v2")
    assert _is_auth_url("https://shop.local/checkout")
    # "author" contains "auth" but is not a credential page.
    assert not _is_auth_url("https://blog.local/author/jane")
    assert not _is_auth_url("https://shop.local/order-history")


# ------------------------------------------------------ messaging heuristic

def _send_history(target: str = "Type a message") -> list[dict[str, Any]]:
    return [{
        "result_ok": True,
        "action": {
            "type": "type",
            "target": {"name": target},
            "params": {"text": "hi", "submit": True},
        },
    }]


def test_messaging_autocomplete_only_on_chat_hosts():
    action = {"type": "type", "target": {"name": "Type a message"}}
    assert _is_messaging_host("https://web.whatsapp.com/")
    assert _is_messaging_host("https://www.messenger.com/t/123")
    assert _auto_complete_after_send(_send_history(), action, "https://web.whatsapp.com/")
    assert not _auto_complete_after_send(_send_history(), action, "https://example.com/search")


def test_messaging_autocomplete_does_not_swallow_search_typing():
    search_action = {"type": "type", "target": {"name": "Search or start new chat"}}
    assert not _auto_complete_after_send(_send_history(), search_action, "https://web.whatsapp.com/")
    send_click = {"type": "click", "target": {"name": "Send"}}
    assert _auto_complete_after_send(_send_history(), send_click, "https://web.whatsapp.com/")


# ------------------------------------------------------------ plan bounds

def _action(**overrides: Any) -> dict[str, Any]:
    base = {
        "type": "scroll",
        "risk": "read",
        "verify": "moved",
        "timeoutMs": 1000,
        "params": {"direction": "down"},
    }
    base.update(overrides)
    return base


def _plan(*actions: dict[str, Any]) -> dict[str, Any]:
    return {"goal": "g", "steps": [{"why": "w", "action": a} for a in actions]}


def test_plan_clamps_too_many_steps():
    """Recovery plans can overshoot the budget; run the leading steps instead
    of aborting the whole mission with PLAN_INVALID. The agent loop still
    enforces the global MAX_STEPS budget."""
    plan = _validate_plan(_plan(*[_action() for _ in range(4)]))

    assert len(plan.steps) == MAX_STEPS_PER_PLAN


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///C:/Windows/win.ini",
    "https://user:pass@example.com/",
    "https:\\\\evil.example",
])
def test_navigation_rejects_unsafe_urls(url: str):
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        _validate_plan(_plan(_action(
            type="navigate", risk="write", params={"url": url},
        )))


def test_click_requires_a_target():
    with pytest.raises(ValueError, match="requires a target"):
        _validate_plan(_plan(_action(type="click", risk="write")))


def test_timeout_bounds_enforced():
    with pytest.raises(ValueError, match="timeoutMs"):
        _validate_plan(_plan(_action(timeoutMs=10)))
    with pytest.raises(ValueError, match="timeoutMs"):
        _validate_plan(_plan(_action(timeoutMs=120_000)))


# --------------------------------------------------------------- hub slots

class FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(message)


class RespondingSocket(FakeSocket):
    """Answers each EXECUTE_ACTION as soon as it is sent."""

    def __init__(self, hub: Hub) -> None:
        super().__init__()
        self.hub = hub

    async def send_json(self, message: dict[str, Any]) -> None:
        await super().send_json(message)
        self.hub.resolve_action(message["taskId"], {"ok": True, "evidence": "done"})


def test_hub_timeout_leaves_no_result_slot():
    hub = Hub()

    async def scenario():
        ws = FakeSocket()
        hub.set_extension(ws)
        res = await hub.send_action("t1", {"type": "click"}, timeout_s=0.05)
        assert res["ok"] is False
        assert res["error"]["code"] == "TIMEOUT"
        assert hub._action_results == {}

    asyncio.run(scenario())


def test_hub_retries_across_live_sockets_and_cleans_up():
    hub = Hub()

    async def scenario():
        dead = FakeSocket(fail=True)
        live = RespondingSocket(hub)
        hub.set_extension(live)
        hub.set_extension(dead)

        res = await hub.send_action("t1", {"type": "click"}, timeout_s=0.5)
        assert res == {"ok": True, "evidence": "done"}
        assert dead not in hub.extension_sockets
        assert hub.extension_ws is live
        assert hub._action_results == {}

    asyncio.run(scenario())


def test_hub_prunes_old_terminal_tasks():
    hub = Hub(max_retained_tasks=3)
    for i in range(4):
        task = hub.new_task(f"t{i}")
        task.state = "completed"
    hub.new_task("new")
    assert len(hub.tasks) <= 4
    assert any(t.instruction == "new" for t in hub.tasks.values())


def test_cancel_aborts_in_flight_action_without_waiting_for_timeout():
    hub = Hub()

    async def scenario():
        ws = FakeSocket()  # accepts the action but never answers
        hub.set_extension(ws)

        async def send():
            return await hub.send_action("task-1", {"type": "click"}, timeout_s=30)

        pending = asyncio.create_task(send())
        for _ in range(50):
            if hub._action_results:
                break
            await asyncio.sleep(0.01)
        assert hub._action_results

        hub.abort_task_actions("task-1")
        res = await asyncio.wait_for(pending, 1)
        assert res["ok"] is False
        assert res["error"]["code"] == "CANCELLED"
        assert hub._action_results == {}

    asyncio.run(scenario())


def test_abort_only_touches_the_cancelled_task():
    hub = Hub()

    async def scenario():
        hub.set_extension(FakeSocket())
        other = asyncio.create_task(hub.send_action("task-2", {"type": "click"}, timeout_s=0.2))
        await asyncio.sleep(0.02)
        hub.abort_task_actions("task-1")
        # The unrelated task still times out normally.
        assert (await other)["error"]["code"] == "TIMEOUT"

    asyncio.run(scenario())


def test_explicit_mock_provider_wins_over_api_keys(monkeypatch):
    from app.providers.mock import MockProvider

    monkeypatch.setattr(agentLoop.config, "PROVIDER", "mock", raising=False)
    monkeypatch.setattr(agentLoop.config, "GROQ_API_KEY", "present-but-ignored", raising=False)
    monkeypatch.setattr(agentLoop.config, "GEMINI_API_KEY", "present-but-ignored", raising=False)
    assert isinstance(agentLoop._provider_for_config(), MockProvider)


def test_requested_provider_falls_back_to_available_key(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "groq", raising=False)
    monkeypatch.setattr(config, "GROQ_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "present-but-secret", raising=False)
    selection = config.resolve_provider()
    assert selection["requested"] == "groq"
    assert selection["effective"] == "gemini"
    assert selection["fallback"] is True
    assert "present-but-secret" not in str(selection)


def test_requested_remote_provider_without_keys_reports_mock(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "groq", raising=False)
    monkeypatch.setattr(config, "GROQ_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "", raising=False)
    selection = config.resolve_provider()
    assert selection["effective"] == "mock"
    assert selection["fallback"] is True
    assert "groq" in str(selection["reason"])


def test_health_reports_effective_provider_and_fallback(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "groq", raising=False)
    monkeypatch.setattr(config, "GROQ_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "", raising=False)
    body = TestClient(app).get("/api/health").json()
    assert body["provider"] == "mock"
    assert body["requestedProvider"] == "groq"
    assert body["providerFallback"] is True
    assert body["providerFallbackReason"]


def test_health_reports_explicit_mock_without_fallback(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "mock", raising=False)
    monkeypatch.setattr(config, "GROQ_API_KEY", "present-but-ignored", raising=False)
    body = TestClient(app).get("/api/health").json()
    assert body["provider"] == "mock"
    assert body["requestedProvider"] == "mock"
    assert body["providerFallback"] is False
    assert "present-but-ignored" not in str(body)


def test_timeout_is_not_retried_for_non_idempotent_actions():
    calls: list[tuple[str, float]] = []

    class Hub:
        async def send_action(self, task_id: str, action: dict[str, Any], timeout_s: float = 45.0):
            calls.append((action["type"], timeout_s))
            return {"ok": False, "error": {"code": "TIMEOUT", "message": "slow", "recoverable": True}}

    click = {"type": "click", "timeoutMs": 1000}
    res = asyncio.run(agentLoop._execute_with_retries(Hub(), click, "t"))
    assert res["error"]["code"] == "TIMEOUT"
    # A click that may still be executing must never be re-sent.
    assert len(calls) == 1
    assert calls[0][1] >= 11.0

    calls.clear()
    asyncio.run(agentLoop._execute_with_retries(Hub(), {"type": "scroll", "timeoutMs": 1000}, "t"))
    assert len(calls) == agentLoop.MAX_RETRIES + 1


def test_loop_rejects_malformed_plan_without_crashing():
    class AnyHub:
        calls = 0

        async def send_action(self, task_id: str, action: dict[str, Any], timeout_s: float = 45.0):
            self.calls += 1
            return {"ok": True, "evidence": "ok"}

    hub = AnyHub()

    async def scenario():
        return await agentLoop.run_with_injection(
            hub,
            planner=lambda *a, **k: {"steps": ["not-a-step"]},
            obs_factory=lambda: _obs(),
            task_id="malformed-plan",
        )

    out = asyncio.run(scenario())
    assert out["final"] == "failed"
    assert "malformed" in out["summary"]
    assert hub.calls == 0
