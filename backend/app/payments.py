"""Payment-provider interface (Task 4) — mock-first, honest.

No real card processing happens in this task. There is deliberately
no live Stripe integration here: integrating a live payments API with
real keys is a separate, reviewed task (see ``docs/CREDENTIALS_PAYMENTS.md``).

What this module does provide:

* :func:`validate_payment` — the policy gate the loop and the API
  enforce before any payment step: every payment requires explicit
  user approval + takeover, regardless of amount, and a payment above
  ``PRIVAGENT_MAX_PAYMENT_AMOUNT`` (``config.MAX_PAYMENT_AMOUNT``) is
  blocked outright unless the operator explicitly raises the limit.
  No currency conversion is performed — the limit applies to the
  amount as stated, in ``config.PAYMENT_CURRENCY``. A payment stated
  in a different currency is treated as above the limit (fail closed)
  unless the operator confirms it in takeover, where they can see the
  real amount in the merchant's own checkout.
* :class:`PaymentProvider` — the interface a real provider must
  implement (Stripe Link-style one-time virtual card: create a
  single-use virtual card, the operator approves it in the provider's
  app, the agent sees the last-4 only). The loop is written against
  this interface, so a real provider can be added later without
  changing the agent loop.
* :class:`MockPaymentProvider` — the default. It never moves money,
  never charges a card, and never sees a real card number: it mints a
  clearly-marked mock virtual card (last-4 ``4242``) for tests and
  demos. Its ``charge`` method raises — a mock must not be able to
  "succeed" at taking money.
* :func:`log_payment_attempt` — the audit trail. Every payment
  attempt is logged (amount, merchant domain, last-4, approval id,
  outcome) to ``backend/data/payments_audit.jsonl`` (mode 0600).
  Card numbers, CVV, and full credentials are never logged — the log
  entry schema has no field for them.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Protocol

from . import config

logger = logging.getLogger("navigator.payments")

__all__ = [
    "PaymentRequest",
    "PaymentDecision",
    "VirtualCard",
    "PaymentProvider",
    "MockPaymentProvider",
    "default_provider",
    "validate_payment",
    "payment_requires_takeover",
    "log_payment_attempt",
    "read_audit_log",
]

_AUDIT_ENV = "PRIVAGENT_PAYMENTS_AUDIT_FILE"


def payment_requires_takeover() -> bool:
    """Payments always require explicit user approval + takeover."""
    return True


@dataclass
class PaymentRequest:
    amount: Decimal
    currency: str
    merchant_domain: str
    description: str = ""
    last4: Optional[str] = None
    approval_id: Optional[str] = None


@dataclass
class PaymentDecision:
    allowed: bool
    reason: str
    requires_takeover: bool = True
    requires_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "requiresTakeover": self.requires_takeover,
            "requiresApproval": self.requires_approval,
        }


def _parse_amount(raw: Any) -> Optional[Decimal]:
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if amount.is_nan() or amount.is_infinite():
        return None
    return amount


def validate_payment(
    amount: Any,
    currency: str = "",
    merchant_domain: str = "",
    *,
    max_amount: Optional[float] = None,
    expected_currency: Optional[str] = None,
) -> PaymentDecision:
    """Policy gate for a payment. Fail closed on every ambiguity.

    * Non-positive or unparsable amounts are blocked.
    * An amount above the configured limit is blocked outright.
    * A currency other than the configured one is blocked (no
      conversion is performed — see the module docstring).
    * Everything that passes still requires approval + takeover.
    """
    limit = Decimal(str(max_amount if max_amount is not None else config.MAX_PAYMENT_AMOUNT))
    expected = (expected_currency or config.PAYMENT_CURRENCY or "").strip().upper()
    parsed = _parse_amount(amount)
    if parsed is None:
        return PaymentDecision(False, "payment amount is not a valid number")
    if parsed <= 0:
        return PaymentDecision(False, "payment amount must be positive")
    currency = (currency or "").strip().upper()
    if not currency:
        return PaymentDecision(False, "payment currency is required")
    if expected and currency != expected:
        return PaymentDecision(
            False,
            f"payment currency {currency} does not match the configured "
            f"payment currency {expected} (no currency conversion is "
            f"performed); confirm the amount yourself in takeover",
        )
    if parsed > limit:
        return PaymentDecision(
            False,
            f"payment of {parsed} {currency} exceeds the configured limit "
            f"of {limit} {expected}; raise PRIVAGENT_MAX_PAYMENT_AMOUNT "
            f"explicitly if you mean to allow it",
        )
    merchant = (merchant_domain or "").strip().lower()
    if not merchant:
        return PaymentDecision(False, "payment merchant domain is required")
    return PaymentDecision(
        True,
        f"payment of {parsed} {currency} to {merchant} is within the "
        f"configured limit; it still requires your approval and takeover",
    )


@dataclass
class VirtualCard:
    """A single-use virtual card. The agent sees the last-4 only."""

    card_id: str
    last4: str
    merchant_domain: str
    amount_limit: Decimal
    currency: str
    status: str = "mock"
    expiry: str = "12/29"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        # Deliberately no full PAN, no CVV, no cardholder secret.
        return {
            "cardId": self.card_id,
            "last4": self.last4,
            "merchantDomain": self.merchant_domain,
            "amountLimit": str(self.amount_limit),
            "currency": self.currency,
            "status": self.status,
            "expiry": self.expiry,
            "createdAt": self.created_at,
        }


class PaymentProvider(Protocol):
    """Interface a real payment provider must implement.

    A real implementation (e.g. Stripe Link-style one-time virtual
    card) must: create a single-use card scoped to one merchant and
    amount, require the operator to approve it in the provider's own
    app, and expose only the last-4 to the agent. It must never return
    a full PAN or CVV to this backend.
    """

    def create_virtual_card(
        self, merchant_domain: str, amount_limit: Any, currency: str
    ) -> VirtualCard: ...

    def approve_virtual_card(self, card_id: str, approved: bool) -> VirtualCard: ...

    def charge(self, card_id: str, amount: Any, currency: str) -> dict[str, Any]: ...


class MockPaymentProvider:
    """Test/demo provider. Never moves money, never charges a card.

    ``create_virtual_card`` mints a mock card (last-4 ``4242``) and
    records it in the audit log. ``charge`` always raises: a mock
    succeeding at taking money would be a lie the rest of the system
    could mistake for a real payment.
    """

    def __init__(self) -> None:
        self._cards: dict[str, VirtualCard] = {}
        self._approved: set[str] = set()

    def create_virtual_card(
        self, merchant_domain: str, amount_limit: Any, currency: str
    ) -> VirtualCard:
        parsed = _parse_amount(amount_limit)
        if parsed is None or parsed <= 0:
            raise ValueError("virtual card amount limit must be a positive number")
        decision = validate_payment(parsed, currency, merchant_domain)
        if not decision.allowed:
            raise ValueError(f"virtual card refused: {decision.reason}")
        card = VirtualCard(
            card_id=uuid.uuid4().hex,
            last4="4242",
            merchant_domain=(merchant_domain or "").strip().lower(),
            amount_limit=parsed,
            currency=(currency or "").strip().upper(),
            status="mock",
        )
        self._cards[card.card_id] = card
        log_payment_attempt(
            amount=parsed,
            currency=card.currency,
            merchant_domain=card.merchant_domain,
            last4=card.last4,
            provider="mock",
            outcome="virtual_card_created",
            approval_id=card.card_id,
        )
        return card

    def approve_virtual_card(self, card_id: str, approved: bool) -> VirtualCard:
        card = self._cards.get(card_id)
        if card is None:
            raise KeyError(f"unknown virtual card {card_id!r}")
        if approved:
            self._approved.add(card_id)
            card.status = "mock-approved"
        else:
            card.status = "mock-rejected"
        log_payment_attempt(
            amount=card.amount_limit,
            currency=card.currency,
            merchant_domain=card.merchant_domain,
            last4=card.last4,
            provider="mock",
            outcome="approved" if approved else "rejected",
            approval_id=card.card_id,
        )
        return card

    def charge(self, card_id: str, amount: Any, currency: str) -> dict[str, Any]:
        raise NotImplementedError(
            "the mock payment provider never charges a card — wire a real "
            "provider (see docs/CREDENTIALS_PAYMENTS.md) to move money"
        )


def default_provider() -> MockPaymentProvider:
    """The only provider shipped in this task. A real provider is a follow-up."""
    return MockPaymentProvider()


# ---------------------------------------------------------------------------
# Audit log (amount / merchant / last-4 / approval id only — never card data)
# ---------------------------------------------------------------------------

def _audit_path() -> Path:
    override = os.environ.get(_AUDIT_ENV, "").strip()
    if override:
        return Path(override)
    try:
        from .secrets import is_pytest
        if is_pytest():
            return Path(tempfile.gettempdir()) / f"privagent-test-payments-{os.getpid()}.jsonl"
    except Exception:
        pass
    path = Path(__file__).resolve().parents[1] / "data" / "payments_audit.jsonl"
    return path


def log_payment_attempt(
    *,
    amount: Any,
    currency: str,
    merchant_domain: str,
    last4: Optional[str] = None,
    provider: str = "mock",
    outcome: str,
    approval_id: Optional[str] = None,
    reason: str = "",
) -> dict[str, Any]:
    """Append one audit entry. Never accepts card numbers or CVV."""
    entry: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "amount": str(amount),
        "currency": (currency or "").strip().upper(),
        "merchantDomain": (merchant_domain or "").strip().lower(),
        "last4": (last4 or "")[-4:] if last4 else None,
        "provider": provider,
        "outcome": outcome,
        "approvalId": approval_id,
        "reason": reason,
    }
    path = _audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning("Could not write payment audit log %s: %s", path, exc)
    return entry


def read_audit_log(limit: int = 50) -> list[dict[str, Any]]:
    """Recent audit entries (newest first). Empty when nothing is logged."""
    path = _audit_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return []
    entries: list[dict[str, Any]] = []
    for line in reversed(lines[-max(limit, 0):] if limit else lines):
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries[:limit] if limit else entries
