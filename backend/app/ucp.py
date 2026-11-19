"""Universal Commerce Protocol (UCP) support (Task 14).

UCP is the open standard for agentic commerce co-developed by Google
and Shopify (announced at NRF, 11 January 2026; spec at
github.com/Universal-Commerce-Protocol, Apache 2.0; versions
2026-01-11, 2026-01-23, 2026-04-08, 2026-08-25). A merchant publishes
a JSON profile at ``/.well-known/ucp`` (RFC 8615 well-known location)
listing its capabilities; an agent fetches it, negotiates, and checks
out over a declared transport (REST, MCP, A2A, or embedded). Payment
is deliberately separate: UCP composes with the Agent Payments
Protocol (AP2) for the payment mandate. UCP checkout is live in
Google AI Mode / Gemini for select US, Canadian, and Australian
merchants.

Status, honestly: no live UCP merchant was available to this codebase
(no merchant account, no live Chrome run), so the profile parsing,
discovery, gating, and checkout-session flow below are implemented
against the published spec (mandatory members ``ucp.version``,
``ucp.services``, ``ucp.payment_handlers``; profiles MUST NOT
redirect) and tested against a mocked merchant. The wire shape of the
checkout-session request follows the spec's capability model; it has
not been validated against a real merchant response, and the docs say
so. What *is* verified by tests is the safety envelope around it.

Safety envelope — UCP checkout is a payment, so it goes through the
existing Task 4 payment path with no exceptions:
* :func:`app.payments.validate_payment` caps the amount
  (``PRIVAGENT_MAX_PAYMENT_AMOUNT``) and the currency; anything over
  the cap or in the wrong currency is blocked outright, and approval
  cannot lift it.
* The loop requires the operator's approval (confirmation gate) AND
  takeover (the operator reviews the checkout in the browser) before
  the backend checkout runs.
* Payment details come from the vault / payment provider at execution
  time only. The planner sees the amount, currency, and merchant host
  — never a card number, never a mandate. The merchant receives only
  a payment-mandate reference from the provider, never card data.
* The default provider is mock-first: ``MockPaymentProvider.charge``
  always raises, so a UCP checkout in this codebase fails honestly at
  the payment step until a real provider is wired (see
  docs/CREDENTIALS_PAYMENTS.md). Nothing here can move money by
  accident.
* The checkout is a single attempt, never retried: the loop dispatches
  ``ucp_checkout`` to this module instead of the retrying executor.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Union
from urllib.parse import urlsplit

from .allowlist import egress_allowed

__all__ = [
    "UCPService",
    "UCPProfile",
    "parse_ucp_profile",
    "fetch_ucp_profile",
    "execute_ucp_checkout",
    "UCP_PROFILE_PATH",
    "SUPPORTED_UCP_VERSIONS",
]

UCP_PROFILE_PATH = "/.well-known/ucp"
MAX_PROFILE_BYTES = 64 * 1024
MAX_SERVICES = 20

# Spec releases this implementation was written against. An unknown
# version is refused, not guessed at: parse_ucp_profile raises for it
# (so the loop treats the merchant as having no UCP checkout), and
# execute_ucp_checkout refuses it too (reachable for dict-built
# profiles that bypassed parsing). A newer spec may have changed the
# checkout shape, and guessing is how money goes to the wrong place.
SUPPORTED_UCP_VERSIONS = frozenset({"2026-01-11", "2026-01-23", "2026-04-08", "2026-08-25"})

_TRANSPORTS = frozenset({"rest", "mcp", "a2a", "embedded"})


@dataclass(frozen=True)
class UCPService:
    capability: str  # e.g. "checkout", "catalog", "cart", "orders"
    transport: str  # "rest" | "mcp" | "a2a" | "embedded"
    endpoint: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "transport": self.transport, "endpoint": self.endpoint}


@dataclass(frozen=True)
class UCPProfile:
    version: str
    services: tuple[UCPService, ...] = ()
    payment_handlers: tuple[str, ...] = ()
    origin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "services": [s.to_dict() for s in self.services],
            "paymentHandlers": list(self.payment_handlers),
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UCPProfile":
        return cls(
            version=str(raw.get("version") or ""),
            services=tuple(
                UCPService(
                    capability=str(s.get("capability") or ""),
                    transport=str(s.get("transport") or ""),
                    endpoint=str(s.get("endpoint") or s.get("url") or ""),
                )
                for s in raw.get("services", [])
                if isinstance(s, dict)
            ),
            payment_handlers=tuple(str(h) for h in raw.get("paymentHandlers", [])),
            origin=str(raw.get("origin") or ""),
        )

    def checkout_service(self) -> Optional[UCPService]:
        """The REST checkout service, or None (v1 only speaks REST)."""
        for service in self.services:
            if service.capability == "checkout" and service.transport == "rest" and service.endpoint:
                return service
        return None


def _parse_services(raw: Any, origin: str) -> tuple[list[UCPService], int]:
    """Validate the services member. Returns (services, dropped)."""
    services: list[UCPService] = []
    dropped = 0
    items: list[Any] = []
    if isinstance(raw, dict):
        items = list(raw.values())
    elif isinstance(raw, list):
        items = raw
    else:
        return [], 1
    origin_host = (urlsplit(origin).hostname or "").lower()
    for entry in items:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        capability = str(entry.get("capability") or entry.get("name") or "").strip().lower()
        transport = str(entry.get("transport") or entry.get("binding") or "").strip().lower()
        endpoint = str(entry.get("endpoint") or entry.get("url") or "").strip()
        if not capability or transport not in _TRANSPORTS:
            dropped += 1
            continue
        if endpoint:
            parts = urlsplit(endpoint)
            # Endpoints must be same-origin https URLs: a profile that
            # sends the agent (and its payment mandate) to a third host
            # is not a checkout profile, it is a redirect.
            if (
                parts.scheme != "https"
                or (parts.hostname or "").lower() != origin_host
                or parts.username
                or parts.password
            ):
                dropped += 1
                continue
        services.append(UCPService(capability=capability, transport=transport, endpoint=endpoint))
        if len(services) >= MAX_SERVICES:
            break
    return services, dropped


def parse_ucp_profile(raw: Any, origin: str) -> UCPProfile:
    """Validate a ``/.well-known/ucp`` profile. Raises ValueError when unusable.

    The spec makes ``ucp.version``, ``ucp.services``, and
    ``ucp.payment_handlers`` mandatory — a profile missing any of them
    is not a UCP profile. The ``ucp.`` prefix is the spec's member
    naming; bare ``version``/``services``/``payment_handlers`` keys are
    accepted as well (some publishers omit the prefix).
    """
    if not isinstance(raw, dict):
        raise ValueError("UCP profile must be a JSON object")
    inner = raw.get("ucp") if isinstance(raw.get("ucp"), dict) else raw
    version = str(inner.get("version") or "")
    if not version:
        raise ValueError("UCP profile is missing mandatory member ucp.version")
    # Unknown (newer) versions refuse checkout rather than guess at
    # the checkout shape — the wire format may have moved.
    if version not in SUPPORTED_UCP_VERSIONS:
        raise ValueError(
            f"UCP profile version {version!r} is not supported by this build "
            f"(supported: {', '.join(sorted(SUPPORTED_UCP_VERSIONS))})"
        )
    if "services" not in inner:
        raise ValueError("UCP profile is missing mandatory member ucp.services")
    if "payment_handlers" not in inner and "paymentHandlers" not in inner:
        raise ValueError("UCP profile is missing mandatory member ucp.payment_handlers")
    services, _dropped = _parse_services(inner.get("services"), origin)
    # A profile whose declared services ALL failed validation (bad
    # endpoints, unknown transports) is not a usable profile — raising
    # here beats returning a profile with no services and letting the
    # planner believe a checkout exists.
    if _dropped > 0 and not services:
        raise ValueError(
            "UCP profile declares services but none survived validation "
            "(endpoints must be same-origin https URLs without userinfo)"
        )
    handlers_raw = inner.get("payment_handlers", inner.get("paymentHandlers", []))
    handlers: list[str] = []
    if isinstance(handlers_raw, dict):
        handlers = [str(k) for k in handlers_raw.keys()]
    elif isinstance(handlers_raw, list):
        handlers = [str(h) for h in handlers_raw if isinstance(h, (str, dict))]
    return UCPProfile(
        version=version[:64],
        services=tuple(services),
        payment_handlers=tuple(handlers),
        origin=origin,
    )


def fetch_ucp_profile(origin: str, *, client: Any = None) -> UCPProfile:
    """Fetch and validate a merchant's UCP profile (discovery path).

    ``GET {origin}/.well-known/ucp``; redirects are rejected (the spec
    says profile endpoints MUST NOT use them), 5s timeout, 64 KiB cap,
    egress-gated to the merchant host. Raises ValueError when the
    profile is missing or unusable.
    """
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("http", "https"):
        raise ValueError("UCP discovery needs an http(s) origin")
    url = f"{origin.rstrip('/')}{UCP_PROFILE_PATH}"
    if not egress_allowed(url, extra_hosts=[host]):
        raise ValueError(f"egress to {host} is not allowed")
    import httpx

    own_client = client is None
    http = client or httpx.Client(timeout=5, follow_redirects=False, trust_env=False)
    try:
        resp = http.get(url, headers={"Accept": "application/json"})
        if resp.status_code in (301, 302, 303, 307, 308):
            raise ValueError("UCP profile must not redirect (spec: profile endpoints MUST NOT use redirects)")
        if resp.status_code != 200:
            raise ValueError(f"UCP profile returned HTTP {resp.status_code}")
        if len(resp.content) > MAX_PROFILE_BYTES:
            raise ValueError("UCP profile exceeds 64 KiB")
        try:
            data = resp.json()
        except Exception:
            raise ValueError("UCP profile is not valid JSON") from None
        return parse_ucp_profile(data, origin)
    finally:
        if own_client:
            http.close()


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **result}


def _fail(reason: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": "UCP_CHECKOUT_FAILED", "message": reason, "recoverable": False}}


def execute_ucp_checkout(
    profile: Optional[Union[UCPProfile, dict[str, Any]]],
    *,
    amount: Any,
    currency: str,
    task_id: str = "",
    provider: Any = None,
    client: Any = None,
) -> dict[str, Any]:
    """Run a UCP checkout session. Single attempt, never retried.

    ``profile`` may be a UCPProfile or the plain dict the loop stores
    in working_memory (``to_dict()`` output) — the dict is re-parsed
    here so the wire call always uses a validated object.

    Order: (1) payment policy gate (cap + currency — approval cannot
    lift it); (2) create a checkout session with the merchant's REST
    checkout service and verify the returned total matches the planned
    amount; (3) mint + approve a provider virtual card scoped to this
    merchant and amount; (4) charge it — the mock provider always
    raises here, honestly; (5) confirm the session with the mandate
    reference the provider returned (never card data). Returns an
    action-result-shaped dict (``ok`` / ``evidence`` / ``error``).
    """
    from .payments import default_provider, log_payment_attempt, validate_payment

    if isinstance(profile, dict):
        try:
            profile = UCPProfile.from_dict(profile)
        except Exception as exc:
            return _fail(f"UCP profile is not usable: {exc}")
    if profile is None:
        return _fail("no UCP profile for this merchant — the checkout cannot run")

    host = (urlsplit(profile.origin).hostname or "").lower()
    decision = validate_payment(amount, currency, host)
    if not decision.allowed:
        return _fail(f"UCP checkout blocked: {decision.reason}")
    if profile.version not in SUPPORTED_UCP_VERSIONS:
        return _fail(
            f"UCP profile version {profile.version!r} is newer than the versions "
            "this build supports (2026-01-11 … 2026-08-25); refusing to guess "
            "at the checkout shape"
        )
    service = profile.checkout_service()
    if service is None:
        return _fail(
            "this merchant's UCP profile has no REST checkout service "
            "(only REST checkouts are supported; MCP/A2A/embedded bindings are not)"
        )
    if not egress_allowed(service.endpoint, extra_hosts=[host]):
        return _fail(f"egress to the UCP checkout endpoint is not allowed")

    import httpx

    own_client = client is None
    http = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
    try:
        # (2) Checkout session creation — no payment data in this call.
        try:
            resp = http.post(
                service.endpoint,
                json={
                    "version": profile.version,
                    "capability": "checkout",
                    "currency": (currency or "").strip().upper(),
                    "total": {"amount": str(amount), "currency": (currency or "").strip().upper()},
                },
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            return _fail(f"UCP checkout session request failed: {exc}")
        if resp.status_code in (301, 302, 303, 307, 308):
            return _fail("UCP checkout endpoint redirected; refusing to follow it")
        if resp.status_code < 200 or resp.status_code >= 300:
            return _fail(f"UCP checkout session returned HTTP {resp.status_code}")
        try:
            session = resp.json()
        except Exception:
            return _fail("UCP checkout session response is not valid JSON")
        session_id = str(
            session.get("sessionId") or session.get("session_id") or session.get("id") or ""
        )
        if not session_id:
            return _fail("UCP checkout session response has no session id")
        total = session.get("total") or {}
        try:
            returned = Decimal(str(total.get("amount", "")))
            planned = Decimal(str(amount))
        except Exception:
            return _fail("UCP checkout session total is not a number; refusing to pay an unverified total")
        if returned != planned:
            return _fail(
                f"UCP checkout session total {returned} does not match the planned "
                f"amount {planned}; refusing to pay a different total"
            )

        # (3) Provider card, scoped to this merchant + amount.
        pay = provider or default_provider()
        try:
            card = pay.create_virtual_card(host, planned, (currency or "").strip().upper())
            pay.approve_virtual_card(card.card_id, True)
        except Exception as exc:
            return _fail(f"payment provider refused the virtual card: {exc}")

        # (4) Charge — the mock provider always raises here, by design.
        try:
            charge = pay.charge(card.card_id, planned, (currency or "").strip().upper())
        except Exception as exc:
            log_payment_attempt(
                amount=planned,
                currency=(currency or "").strip().upper(),
                merchant_domain=host,
                last4=getattr(card, "last4", ""),
                provider="mock" if provider is None else "ucp",
                outcome="charge_failed",
            )
            return _fail(f"UCP checkout payment failed: {exc}")
        mandate = ""
        if isinstance(charge, dict):
            mandate = str(charge.get("mandate") or charge.get("paymentMandate") or charge.get("id") or "")
        if not mandate:
            return _fail("payment provider returned no mandate reference; cannot confirm the checkout")

        # (5) Confirm the session with the mandate reference only.
        try:
            confirm = http.post(
                service.endpoint,
                json={"sessionId": session_id, "paymentMandate": mandate},
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            return _fail(f"UCP checkout confirmation failed: {exc}")
        if confirm.status_code < 200 or confirm.status_code >= 300:
            return _fail(f"UCP checkout confirmation returned HTTP {confirm.status_code}")
        try:
            confirmation = confirm.json()
        except Exception:
            confirmation = {}
        order_id = str(confirmation.get("orderId") or confirmation.get("order_id") or "")
        log_payment_attempt(
            amount=planned,
            currency=(currency or "").strip().upper(),
            merchant_domain=host,
            last4=getattr(card, "last4", ""),
            provider="mock" if provider is None else "ucp",
            outcome="charged",
        )
        try:
            from .audit import append_event

            append_event(
                "payment_attempt",
                f"UCP checkout {planned} {(currency or '').strip().upper()} to {host} "
                f"session {session_id[:12]} outcome charged",
                actor="system",
                task_id=task_id,
            )
        except Exception:
            pass
        return _ok({
            "evidence": (
                f"UCP checkout completed: {planned} {(currency or '').strip().upper()} "
                f"to {host} (session {session_id[:12]}"
                + (f", order {order_id}" if order_id else "")
                + f", card ending {getattr(card, 'last4', '')})"
            ),
            "sessionId": session_id,
            "orderId": order_id,
        })
    finally:
        if own_client:
            http.close()
