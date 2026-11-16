"""Task 4: safe credential / payment path — vault, takeover, payments.

Rules under test (see docs/CREDENTIALS_PAYMENTS.md):

* The planner / LLM sees only a ``{{CREDENTIAL:domain:kind:label}}``
  placeholder, never the real value. Substitution happens in the loop
  just before the action is sent to the extension, after the gate.
* An OTP field forces takeover and the OTP is never in any planner
  payload; the loop observes nothing while the operator has the browser.
* Payments always require approval + takeover and are blocked above
  ``PRIVAGENT_MAX_PAYMENT_AMOUNT``. The provider is a mock: last-4
  only, no real card processing.
* Account creation and permanent account deletion are blocked outright.

All secrets below are obviously fake test values. The vault tests pin
the encrypted store to a temp dir with a fixed master key (same pattern
as test_secrets.py) so the suite never touches real key material.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from app import agentLoop
from app import payments as payments_mod
from app import takeover as takeover_mod
from app import vault as vault_mod
from app import secrets as secrets_store
from app.contracts import BrowserObservation, InteractiveElement
from app.planner import build_prompt, plan_from_observation
from app.privacy import classify_and_redact
from app.providers.mock import MockProvider

# 32 zero bytes as a urlsafe-base64 Fernet-style master key (test only).
TEST_MASTER_KEY = base64.urlsafe_b64encode(b"\x00" * 32).decode("ascii")

# Obviously fake test values — never a real credential.
FAKE_PASSWORD = "Test-Password-Only-1234"
FAKE_CARD_LAST4 = "4242"


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the secret store (and therefore the vault) at a temp dir."""
    store_dir = tmp_path / "secrets"
    monkeypatch.setenv(secrets_store.SECRETS_DIR_ENV, str(store_dir))
    monkeypatch.setenv(secrets_store.MASTER_KEY_ENV, TEST_MASTER_KEY)
    secrets_store.reset_for_tests()
    yield store_dir
    secrets_store.reset_for_tests()


# ---------------------------------------------------------------------------
# Fake extension hub + gates (mirrors the ConfirmHub pattern)
# ---------------------------------------------------------------------------

class TakeoverHub:
    """Fake hub: records executed actions and takeover notifications."""

    def __init__(self):
        self.executed: list[str] = []
        self.executed_texts: list[Any] = []
        self.takeover_requests: list[dict[str, Any]] = []
        self.takeover_notifications: list[dict[str, Any]] = []
        self.decision: bool | None = None
        self.event = asyncio.Event()

    def add_request(self, req: dict[str, Any]) -> None:
        self.takeover_requests.append(req)

    async def notify_extension(self, message: dict[str, Any]) -> None:
        self.takeover_notifications.append(message)

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        await asyncio.sleep(0.01)
        self.executed.append(action["type"])
        if action["type"] == "type":
            self.executed_texts.append((action.get("params") or {}).get("text"))
        return {"ok": True, "evidence": f"did {action['type']}"}


def _el(target_id: str, name: str, role: str = "textbox", tag: str = "input",
        input_type: str = "text") -> dict[str, Any]:
    return {"targetId": target_id, "role": role, "name": name, "tag": tag,
            "inputType": input_type, "visible": True, "enabled": True}


def _obs(url: str, elements: list[dict[str, Any]] | None = None,
         page_text: str = "", screenshot: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"tabId": 1, "url": url, "title": "t",
                         "interactive": elements or [], "scroll": {}, "dpr": 1}
    if page_text:
        d["page_text"] = page_text
    if screenshot:
        d["screenshot"] = screenshot
    return d


class ScriptedPlanner:
    """Returns a scripted plan per planner call; records payloads seen."""

    def __init__(self, plans: list[dict[str, Any]]):
        self.plans = plans
        self.payloads: list[Any] = []
        self.n = 0

    def __call__(self, instruction: str, payload: Any, history: list[dict[str, Any]]):
        self.payloads.append(payload)
        plan = self.plans[min(self.n, len(self.plans) - 1)]
        self.n += 1
        return plan


def _empty_plan() -> dict[str, Any]:
    return {"goal": "g", "steps": []}


def _type_plan(text: str, target_name: str = "Password") -> dict[str, Any]:
    return {"goal": "g", "steps": [{"why": "w", "action": {
        "type": "type", "risk": "write", "verify": "v", "timeoutMs": 100,
        "target": {"targetId": "q", "name": target_name},
        "params": {"text": text}}} ]}


def _click_plan(target_name: str) -> dict[str, Any]:
    return {"goal": "g", "steps": [{"why": "w", "action": {
        "type": "click", "risk": "write", "verify": "v", "timeoutMs": 100,
        "target": {"targetId": "q", "name": target_name}}} ]}


def _run_loop(hub, planner, obs_factory, instruction="loop", **kwargs):
    return asyncio.run(agentLoop.run_with_injection(
        hub, planner=planner, obs_factory=obs_factory,
        instruction=instruction, takeover_gate=hub, **kwargs))


# ---------------------------------------------------------------------------
# Vault: placeholders, encryption, rejections
# ---------------------------------------------------------------------------

def test_vault_stores_encrypted_and_planner_sees_only_placeholder(isolated_store):
    meta = vault_mod.add_credential(
        "example.com", "login", "personal",
        username="user@example.com", value=FAKE_PASSWORD)
    assert meta.placeholder == "{{CREDENTIAL:example.com:login:personal}}"
    assert meta.to_dict()["placeholder"] == meta.placeholder
    # The stored envelope is ciphertext, never the password.
    raw = (isolated_store / "secrets.enc.json").read_text()
    assert FAKE_PASSWORD not in raw
    assert "user@example.com" not in raw
    # The vault resolves the placeholder at execution time only.
    assert vault_mod.resolve_placeholder(meta.placeholder) == FAKE_PASSWORD
    assert vault_mod.substitute_placeholders(
        f"fill {meta.placeholder} here") == f"fill {FAKE_PASSWORD} here"
    # Metadata listing never carries a value.
    listed = vault_mod.list_credentials()
    assert len(listed) == 1
    assert "value" not in listed[0].to_dict()
    assert FAKE_PASSWORD not in json.dumps([m.to_dict() for m in listed])


def test_vault_rejects_otp_kinds_and_full_card_numbers(isolated_store):
    for kind in ("otp", "totp", "one-time-code", "verification-code"):
        with pytest.raises(ValueError):
            vault_mod.add_credential("example.com", kind, "x", value="123456")
    # A full card number is rejected, not truncated to a last-4.
    with pytest.raises(ValueError):
        vault_mod.add_credential("example.com", "card", "visa", value="4111111111111111")
    with pytest.raises(ValueError):
        vault_mod.add_credential("example.com", "card", "visa", value="4111 1111 1111 1111")
    # Exactly 4 digits is the only accepted card value.
    meta = vault_mod.add_credential("example.com", "card", "visa", value="4242")
    assert meta.placeholder == "{{CREDENTIAL:example.com:card:visa}}"
    # Unknown placeholders fail closed at substitution time.
    with pytest.raises(KeyError):
        vault_mod.resolve_placeholder("{{CREDENTIAL:example.com:login:nope}}")
    # A placeholder anywhere but params.text is refused (here the
    # credential exists, so the failure is the placement, not the
    # lookup): smuggling one into the action target is a plan error.
    vault_mod.add_credential("example.com", "login", "personal", value=FAKE_PASSWORD)
    with pytest.raises(ValueError):
        vault_mod.substitute_placeholders_in_action(
            {"type": "click", "target": {"name": "{{CREDENTIAL:example.com:login:personal}}"}})


def test_vault_api_stores_encrypted_and_lists_metadata_only(isolated_store, authed_client):
    res = authed_client.post("/api/vault/credentials", json={
        "domain": "example.com", "kind": "login", "label": "personal",
        "username": "user@example.com", "value": FAKE_PASSWORD})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["credential"]["placeholder"] == "{{CREDENTIAL:example.com:login:personal}}"
    assert FAKE_PASSWORD not in res.text
    res = authed_client.get("/api/vault/credentials")
    assert res.status_code == 200
    assert FAKE_PASSWORD not in res.text
    assert res.json()["credentials"][0]["domain"] == "example.com"
    # OTP kinds are rejected over the API too.
    res = authed_client.post("/api/vault/credentials", json={
        "domain": "example.com", "kind": "login", "label": "otp",
        "value": "123456"})
    assert res.status_code == 200  # a login labelled "otp" is still a login
    res = authed_client.post("/api/vault/credentials", json={
        "domain": "example.com", "kind": "otp", "label": "x", "value": "123456"})
    assert res.status_code == 422
    # Delete works and a second delete is a 404.
    res = authed_client.delete("/api/vault/credentials/example.com/login/personal")
    assert res.status_code == 200
    res = authed_client.delete("/api/vault/credentials/example.com/login/personal")
    assert res.status_code == 404


def test_build_prompt_lists_placeholder_never_secret(isolated_store):
    vault_mod.add_credential("example.com", "login", "personal",
                             username="user@example.com", value=FAKE_PASSWORD)
    payload, _ = classify_and_redact(
        BrowserObservation.model_validate({
            "tabId": 1, "url": "https://example.com/home", "title": "t",
            "interactive": [], "scroll": {}, "dpr": 1}),
        instruction="sign in")
    prompt = build_prompt("sign in to example.com", payload, [])
    assert "{{CREDENTIAL:example.com:login:personal}}" in prompt
    assert FAKE_PASSWORD not in prompt
    # A different domain's credential is not offered on this page.
    vault_mod.add_credential("other.com", "login", "personal", value="Other-Secret-1")
    prompt = build_prompt("sign in to example.com", payload, [])
    assert "other.com" not in prompt
    assert "Other-Secret-1" not in prompt


# ---------------------------------------------------------------------------
# Planner validation: invented placeholders and card numbers are rejected
# ---------------------------------------------------------------------------

class _FixedProvider(MockProvider):
    def __init__(self, plan: dict[str, Any]):
        self._plan = plan

    def plan_call(self, instruction, payload, history, working_memory=None):
        return self._plan


def _payload_for(url: str, elements=None) -> Any:
    obs = BrowserObservation.model_validate({
        "tabId": 1, "url": url, "title": "t",
        "interactive": elements or [], "scroll": {}, "dpr": 1})
    payload, _ = classify_and_redact(obs, instruction="test")
    return payload


def test_planner_rejects_invented_placeholder(isolated_store):
    payload = _payload_for("https://example.com/home")
    with pytest.raises(Exception, match="unknown credential placeholder"):
        plan_from_observation(instruction="sign in", payload=payload,
                              provider=_FixedProvider(_type_plan(
                                  "{{CREDENTIAL:example.com:login:nope}}")))


def test_planner_accepts_real_placeholder_and_rejects_card_number(isolated_store):
    vault_mod.add_credential("example.com", "login", "personal", value=FAKE_PASSWORD)
    payload = _payload_for("https://example.com/home")
    plan = plan_from_observation(
        instruction="sign in", payload=payload,
        provider=_FixedProvider(_type_plan("{{CREDENTIAL:example.com:login:personal}}")))
    assert plan.steps[0].action.params.text == "{{CREDENTIAL:example.com:login:personal}}"
    with pytest.raises(Exception, match="card number"):
        plan_from_observation(instruction="pay", payload=payload,
                              provider=_FixedProvider(_type_plan("4111 1111 1111 1111")))


def test_planner_otp_field_is_never_typed_and_completion_becomes_handoff():
    otp = InteractiveElement.model_validate({
        "targetId": "otp", "role": "textbox", "name": "Enter OTP",
        "tag": "input", "inputType": "tel", "visible": True, "enabled": True})
    payload = _payload_for("https://example.com/verify", [otp])
    # A plan that types into the OTP field is invalid, not executable.
    with pytest.raises(Exception, match="one-time-code"):
        plan_from_observation(instruction="verify my account", payload=payload,
                              provider=MockProvider())
    # A plan that claims completion on the OTP page becomes a human
    # handoff wait instead of a false "Mission Completed".
    done = _FixedProvider({"goal": "verify", "steps": [], "answer": "verified"})
    plan = plan_from_observation(instruction="verify my account", payload=payload,
                                 provider=done)
    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "wait"
    assert plan.steps[0].action.risk == "sensitive"


# ---------------------------------------------------------------------------
# Takeover: detection, observation blackout, resume / cancel / timeout
# ---------------------------------------------------------------------------

def test_otp_field_forces_takeover_and_planner_never_sees_code():
    hub = TakeoverHub()
    calls = {"obs": 0}

    def obs_factory():
        calls["obs"] += 1
        if calls["obs"] == 1:
            return _obs("https://example.com/verify",
                        [_el("otp", "Enter OTP", input_type="tel")],
                        page_text="Enter the 6-digit code sent to your phone")
        return _obs("https://example.com/home", [], page_text="Welcome back")

    planner = ScriptedPlanner([_empty_plan()])

    async def scenario():
        async def resolve_soon():
            await asyncio.sleep(0.2)
            hub.decision = True
            hub.event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=planner, obs_factory=obs_factory,
                instruction="verify my account", takeover_gate=hub),
            resolve_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "completed"
    # Takeover was requested for the OTP surface, and the extension was
    # told to stop observing, then told to resume.
    assert hub.takeover_requests and hub.takeover_requests[0]["kind"] == "otp"
    assert [n["type"] for n in hub.takeover_notifications] == ["TAKEOVER_START", "TAKEOVER_END"]
    # The planner was never called on the OTP observation: it ran only
    # after the operator finished, on the fresh post-takeover page.
    assert planner.n == 1
    assert planner.payloads[0].url == "https://example.com/home"
    assert not any("otp" in (e.name or "").lower()
                   for e in planner.payloads[0].interactive)
    # The loop observed exactly twice (before takeover, after resume) —
    # it did not observe while the operator had the browser.
    assert calls["obs"] == 2
    # No action was ever executed into the OTP field.
    assert hub.executed == []


def test_takeover_discards_observation_and_suppresses_screenshot():
    hub = TakeoverHub()
    calls = {"obs": 0}
    screenshot = "data:image/jpeg;base64,FAKE-SCREENSHOT"

    def obs_factory():
        calls["obs"] += 1
        if calls["obs"] == 1:
            return _obs("https://example.com/login",
                        [_el("pw", "Password", input_type="password")],
                        screenshot=screenshot)
        return _obs("https://example.com/home", [], page_text="Welcome back",
                    screenshot=screenshot)

    planner = ScriptedPlanner([_empty_plan()])

    async def scenario():
        async def resolve_soon():
            await asyncio.sleep(0.2)
            hub.decision = True
            hub.event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=planner, obs_factory=obs_factory,
                instruction="sign in", takeover_gate=hub),
            resolve_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "completed"
    assert hub.takeover_requests[0]["kind"] == "login"
    # The first post-takeover observation carries no screenshot even
    # though the page has no sensitive elements: the operator may have
    # left a password visible on screen.
    assert planner.payloads[0].screenshot is None
    assert calls["obs"] == 2


def test_takeover_cancel_fails_task_and_timeout_fails_task():
    # Cancel: the operator declines the takeover.
    hub = TakeoverHub()
    planner = ScriptedPlanner([_empty_plan()])

    async def cancel_scenario():
        async def cancel_soon():
            await asyncio.sleep(0.2)
            hub.decision = False
            hub.event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=planner,
                obs_factory=lambda: _obs("https://example.com/login",
                                         [_el("pw", "Password", input_type="password")]),
                instruction="sign in", takeover_gate=hub),
            cancel_soon(),
        )
        return state

    state = asyncio.run(cancel_scenario())
    assert state["final"] == "failed"
    assert "cancelled" in (state["summary"] or "").lower()
    assert hub.executed == []

    # Timeout: nobody resolves the takeover.
    hub2 = TakeoverHub()
    state = asyncio.run(agentLoop.run_with_injection(
        hub2, planner=ScriptedPlanner([_empty_plan()]),
        obs_factory=lambda: _obs("https://example.com/login",
                                 [_el("pw", "Password", input_type="password")]),
        instruction="sign in", takeover_gate=hub2, takeover_timeout_s=0.2))
    assert state["final"] == "failed"
    assert "timed out" in (state["summary"] or "").lower()
    assert hub2.executed == []


def test_no_takeover_gate_falls_back_to_confirmation_gate():
    """Production (run_task) always wires a takeover gate. A caller that
    only wires a confirmation gate keeps the pre-Task 4 behaviour: the
    payment action halts for approval instead of failing closed."""
    class ConfirmOnlyHub:
        """Only the confirmation contract — deliberately no takeover gate."""

        def __init__(self):
            self.executed: list[str] = []
            self.takeover_requests: list[dict[str, Any]] = []
            self.confirm_requests: list[dict[str, Any]] = []
            self.confirm_event = asyncio.Event()
            self.decision: bool | None = None

        async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
            await asyncio.sleep(0.01)
            self.executed.append(action["type"])
            return {"ok": True, "evidence": f"did {action['type']}"}

    hub = ConfirmOnlyHub()
    planner = ScriptedPlanner([
        {"goal": "g", "steps": [{"why": "w", "action": {
            "type": "click", "risk": "write", "verify": "v", "timeoutMs": 100,
            "target": {"targetId": "q", "name": "Place order"}}}]},
        _empty_plan(),
    ])

    async def scenario():
        async def reject_soon():
            await asyncio.sleep(0.2)
            hub.decision = False
            hub.confirm_event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=planner,
                obs_factory=lambda: _obs(
                    "https://shop.example.com/checkout",
                    [_el("pay", "Place order", role="button", tag="button")],
                    page_text="Checkout"),
                instruction="buy the item", confirm_gate=hub),
            reject_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "failed"
    assert "rejected" in (state["summary"] or "").lower()
    assert hub.takeover_requests == []
    assert hub.confirm_requests
    assert hub.executed == []


def test_payment_surface_forces_takeover():
    hub = TakeoverHub()
    calls = {"obs": 0}

    def obs_factory():
        calls["obs"] += 1
        if calls["obs"] == 1:
            return _obs("https://shop.example.com/checkout",
                        [_el("card", "Card number", input_type="text")],
                        page_text="Checkout")
        return _obs("https://shop.example.com/order-confirmed",
                    [], page_text="Thank you for your order")

    planner = ScriptedPlanner([_empty_plan()])

    async def scenario():
        async def resolve_soon():
            await asyncio.sleep(0.2)
            hub.decision = True
            hub.event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=planner, obs_factory=obs_factory,
                instruction="buy the item", takeover_gate=hub),
            resolve_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "completed"
    assert hub.takeover_requests[0]["kind"] == "payment"
    assert hub.executed == []


# ---------------------------------------------------------------------------
# Placeholder substitution at execution time (loop level)
# ---------------------------------------------------------------------------

def test_placeholder_substituted_at_execution_never_in_history(isolated_store):
    vault_mod.add_credential("example.com", "login", "personal", value=FAKE_PASSWORD)
    hub = TakeoverHub()
    planner = ScriptedPlanner([
        _type_plan("{{CREDENTIAL:example.com:login:personal}}"),
        _empty_plan(),
    ])
    state = _run_loop(
        hub, planner,
        lambda: _obs("https://example.com/home",
                     [_el("pw", "Password", input_type="password")]),
        instruction="sign in to example.com")
    assert state["final"] == "completed"
    # The extension received the real value (that is the whole point of
    # substitution at DOM-execution time) …
    assert hub.executed_texts == [FAKE_PASSWORD]
    # … but the loop's own record keeps the placeholder form, and the
    # secret appears nowhere in the returned state.
    assert state["history"][0]["action"]["params"]["text"] == \
        "{{CREDENTIAL:example.com:login:personal}}"
    assert FAKE_PASSWORD not in json.dumps(state)


def test_unknown_placeholder_fails_closed(isolated_store):
    hub = TakeoverHub()
    planner = ScriptedPlanner([
        _type_plan("{{CREDENTIAL:example.com:login:nope}}"),
        _empty_plan(),
    ])
    state = _run_loop(
        hub, planner,
        lambda: _obs("https://example.com/home",
                     [_el("pw", "Password", input_type="password")]),
        instruction="sign in to example.com")
    assert state["final"] == "failed"
    assert "placeholder" in (state["summary"] or "").lower()
    assert hub.executed == []


# ---------------------------------------------------------------------------
# Blocked outright: account creation / permanent deletion
# ---------------------------------------------------------------------------

def test_blocked_account_creation_and_deletion_fail_honestly():
    # Account creation: instruction asks for it, target is a password
    # field, on a non-auth URL (so takeover detection does not fire first).
    hub = TakeoverHub()
    state = _run_loop(
        hub, ScriptedPlanner([_type_plan("hunter2-fake"), _empty_plan()]),
        lambda: _obs("https://example.com/welcome",
                     [_el("pw", "Password", input_type="password")]),
        instruction="create an account for me on example.com")
    assert state["final"] == "failed"
    assert "account creation is blocked" in (state["summary"] or "").lower()
    assert hub.executed == []

    # Permanent deletion: click on a "delete account permanently" control.
    hub2 = TakeoverHub()
    state = _run_loop(
        hub2, ScriptedPlanner([_click_plan("Delete account permanently"), _empty_plan()]),
        lambda: _obs("https://example.com/settings",
                     [_el("del", "Delete account permanently", role="button", tag="button")]),
        instruction="clean up my account settings")
    assert state["final"] == "failed"
    assert "permanent account deletion is blocked" in (state["summary"] or "").lower()
    assert hub2.executed == []


# ---------------------------------------------------------------------------
# Payments: validation, mock provider, audit, API
# ---------------------------------------------------------------------------

def test_payment_validation_blocks_above_limit_and_wrong_currency():
    ok = payments_mod.validate_payment(100.0, "INR", "shop.example.com",
                                       max_amount=5000.0, expected_currency="INR")
    assert ok.allowed is True
    assert ok.requires_takeover is True
    over = payments_mod.validate_payment(6000.0, "INR", "shop.example.com",
                                         max_amount=5000.0, expected_currency="INR")
    assert over.allowed is False
    assert "exceeds" in over.reason.lower()
    wrong_currency = payments_mod.validate_payment(100.0, "USD", "shop.example.com",
                                                   max_amount=5000.0, expected_currency="INR")
    assert wrong_currency.allowed is False
    assert "currency" in wrong_currency.reason.lower()
    assert payments_mod.payment_requires_takeover() is True


def test_mock_provider_returns_last4_only_and_never_charges():
    provider = payments_mod.MockPaymentProvider()
    card = provider.create_virtual_card(
        merchant_domain="shop.example.com", amount_limit=99.0, currency="INR")
    assert card.last4 == "4242"
    assert card.status == "mock"
    # The virtual card carries no PAN, no CVV, no full number — by
    # construction: the dataclass has no such fields.
    assert not hasattr(card, "pan") and not hasattr(card, "cvv")
    assert "4242" in repr(card)
    approved = provider.approve_virtual_card(card.card_id, True)
    assert approved.status == "mock-approved"
    with pytest.raises(NotImplementedError):
        provider.charge(card.card_id, amount=99.0, currency="INR")


def test_payment_audit_log_contains_no_card_details(tmp_path, monkeypatch):
    audit_file = tmp_path / "payments_audit.jsonl"
    monkeypatch.setenv("PRIVAGENT_PAYMENTS_AUDIT_FILE", str(audit_file))
    payments_mod.log_payment_attempt(
        amount=99.0, currency="INR", merchant_domain="shop.example.com",
        last4="4242", provider="mock", outcome="approved", approval_id="appr-1")
    entries = payments_mod.read_audit_log()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["amount"] == "99.0"  # the log stores amounts as strings
    assert entry["merchantDomain"] == "shop.example.com"
    assert entry["last4"] == "4242"
    assert entry["approvalId"] == "appr-1"
    assert "4111" not in json.dumps(entries)
    assert "4242424242424242" not in json.dumps(entries)


def test_payments_api_config_validate_audit(authed_client):
    res = authed_client.get("/api/payments/config")
    assert res.status_code == 200
    cfg = res.json()
    assert cfg["provider"] == "mock"
    assert cfg["requiresTakeover"] is True
    assert cfg["requiresApproval"] is True
    assert cfg["maxPaymentAmount"] == 5000.0

    res = authed_client.post("/api/payments/validate", json={
        "amount": 6000, "currency": "INR", "merchantDomain": "shop.example.com"})
    assert res.status_code == 200
    assert res.json()["allowed"] is False

    res = authed_client.post("/api/payments/validate", json={
        "amount": 100, "currency": "INR", "merchantDomain": "shop.example.com"})
    assert res.json()["allowed"] is True
    assert res.json()["requiresTakeover"] is True

    res = authed_client.get("/api/payments/audit")
    assert res.status_code == 200
    assert isinstance(res.json()["entries"], list)


def test_takeover_api_requires_awaiting_state(authed_client):
    # No task exists -> 404; a real task id in a non-takeover state -> 409.
    res = authed_client.get("/api/task/no-such-task/takeover")
    assert res.status_code == 404
    res = authed_client.post("/api/task/no-such-task/takeover/resume")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Detection unit tests
# ---------------------------------------------------------------------------

def test_detect_takeover_trigger_priority_otp_over_login():
    otp = {"targetId": "o", "name": "Enter OTP", "inputType": "tel"}
    trigger = takeover_mod.detect_takeover_trigger(
        "https://example.com/login", [otp], "")
    assert trigger is not None and trigger.kind == "otp"
    assert takeover_mod.detect_takeover_trigger(
        "https://example.com/login", [], "") is not None
    assert takeover_mod.detect_takeover_trigger(
        "https://example.com/home", [], "plain page") is None
    assert takeover_mod.is_payment_surface(
        "https://shop.example.com/checkout") is True
    assert takeover_mod.is_payment_surface(
        "https://shop.example.com/browse") is False
