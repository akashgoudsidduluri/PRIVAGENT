"""Task 14: UCP (Universal Commerce Protocol) tests.

Detection parses the merchant profile (mandatory version/services/
payment_handlers, same-origin https endpoints, supported versions
only). Checkout runs the Task 4 payment path: cap + currency gate,
single attempt, mock provider fails honestly at the charge step, and
no card data ever reaches the planner or the merchant session.
"""
from __future__ import annotations

import httpx
import pytest

from app.ucp import (
    SUPPORTED_UCP_VERSIONS,
    UCPProfile,
    execute_ucp_checkout,
    fetch_ucp_profile,
    parse_ucp_profile,
)

ORIGIN = "https://shop.example.com"


def _profile_raw(**kw):
    d = {
        "ucp": {
            "version": "2026-04-08",
            "services": [
                {
                    "capability": "checkout",
                    "transport": "rest",
                    "endpoint": "https://shop.example.com/ucp/checkout",
                }
            ],
            "payment_handlers": ["card"],
        }
    }
    d.update(kw)
    return d


def _err(out):
    """The executor returns the action-result shape: error is a dict."""
    e = out.get("error")
    return e.get("message", "") if isinstance(e, dict) else str(e or "")


def test_parse_valid_profile():
    prof = parse_ucp_profile(_profile_raw(), ORIGIN)
    assert prof is not None
    assert prof.version == "2026-04-08"
    assert prof.checkout_service() is not None
    assert prof.checkout_service().endpoint == "https://shop.example.com/ucp/checkout"


def test_parse_bare_keys_accepted():
    prof = parse_ucp_profile(
        {
            "version": "2026-01-11",
            "services": [{"capability": "checkout", "transport": "rest",
                          "endpoint": "https://shop.example.com/ucp/checkout"}],
            "payment_handlers": ["card"],
        },
        ORIGIN,
    )
    assert prof is not None and prof.version == "2026-01-11"


def test_parse_rejects_missing_mandatory_fields():
    # The parser raises (fail-closed) instead of returning None.
    with pytest.raises(ValueError):
        parse_ucp_profile({"ucp": {"services": [], "payment_handlers": []}}, ORIGIN)
    with pytest.raises(ValueError):
        parse_ucp_profile({"ucp": {"version": "2026-04-08", "payment_handlers": []}}, ORIGIN)
    with pytest.raises(ValueError):
        parse_ucp_profile({"ucp": {"version": "2026-04-08", "services": []}}, ORIGIN)
    with pytest.raises(ValueError):
        parse_ucp_profile("not json", ORIGIN)


def test_parse_rejects_cross_origin_and_http_endpoints():
    bad = _profile_raw()
    bad["ucp"]["services"][0]["endpoint"] = "https://evil.example.com/ucp/checkout"
    with pytest.raises(ValueError):
        parse_ucp_profile(bad, ORIGIN)

    bad2 = _profile_raw()
    bad2["ucp"]["services"][0]["endpoint"] = "http://shop.example.com/ucp/checkout"
    with pytest.raises(ValueError):
        parse_ucp_profile(bad2, ORIGIN)

    bad3 = _profile_raw()
    bad3["ucp"]["services"][0]["endpoint"] = "https://shop.example.com@evil.example.com/x"
    with pytest.raises(ValueError):
        parse_ucp_profile(bad3, ORIGIN)


def test_parse_rejects_unsupported_version():
    future = _profile_raw()
    future["ucp"]["version"] = "2027-01-01"
    with pytest.raises(ValueError, match="version"):
        parse_ucp_profile(future, ORIGIN)


@pytest.mark.parametrize("version", sorted(SUPPORTED_UCP_VERSIONS))
def test_all_supported_versions_parse(version):
    raw = _profile_raw()
    raw["ucp"]["version"] = version
    assert parse_ucp_profile(raw, ORIGIN) is not None


def test_fetch_profile_same_origin():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/ucp"
        return httpx.Response(200, json=_profile_raw())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    prof = fetch_ucp_profile(ORIGIN, client=client)
    assert prof is not None and prof.version == "2026-04-08"


def test_fetch_profile_rejects_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/ucp"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(ValueError, match="redirect"):
        fetch_ucp_profile(ORIGIN, client=client)


def _mock_client_with_checkout(*, total: str = "1299.00"):
    """MockTransport that answers the checkout-session POST then the confirm POST."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path == "/ucp/checkout" and request.method == "POST":
            # The confirm call POSTs to the same endpoint with a
            # sessionId + paymentMandate in the body; session creation
            # posts a version/capability/total instead.
            try:
                body = request.json()
            except Exception:
                body = {}
            if body.get("sessionId") and body.get("paymentMandate"):
                return httpx.Response(200, json={"status": "confirmed", "orderId": "ord_1"})
            return httpx.Response(200, json={
                "sessionId": "sess_123",
                "total": {"amount": total, "currency": "INR"},
            })
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_checkout_fails_honestly_at_mock_charge():
    """The mock payment provider always raises on charge (Task 4) —
    so a UCP checkout with the default provider fails at the payment
    step, after the session was created and the total verified. The
    failure is reported, not hidden, and no card data was sent."""
    prof = parse_ucp_profile(_profile_raw(), ORIGIN)
    client, calls = _mock_client_with_checkout()
    out = execute_ucp_checkout(prof, amount="1299.00", currency="INR", task_id="t1", client=client)
    assert out["ok"] is False
    assert "charge" in _err(out).lower() or "payment" in _err(out).lower()
    # The checkout session WAS created (one POST) — the failure is at
    # the payment step, not at discovery.
    assert calls["n"] == 1


def test_checkout_refuses_total_mismatch():
    """A merchant that returns a different total than the planned
    amount aborts before any payment is attempted."""
    prof = parse_ucp_profile(_profile_raw(), ORIGIN)
    client, calls = _mock_client_with_checkout(total="9999.00")
    out = execute_ucp_checkout(prof, amount="1299.00", currency="INR", task_id="t1", client=client)
    assert out["ok"] is False
    assert "total" in _err(out).lower()
    assert calls["n"] == 1  # session created, then aborted — no charge, no confirm


def test_checkout_cap_is_a_hard_block():
    prof = parse_ucp_profile(_profile_raw(), ORIGIN)
    client, calls = _mock_client_with_checkout()
    out = execute_ucp_checkout(prof, amount="999999.00", currency="INR", task_id="t1", client=client)
    assert out["ok"] is False
    assert "cap" in _err(out).lower() or "exceed" in _err(out).lower()
    assert calls["n"] == 0  # blocked before any network call


def test_checkout_needs_a_profile_and_a_checkout_service():
    out = execute_ucp_checkout(None, amount="10.00", currency="INR", task_id="t1")
    assert out["ok"] is False

    raw = _profile_raw()
    raw["ucp"]["services"] = [{"capability": "discovery", "transport": "rest",
                               "endpoint": "https://shop.example.com/ucp/discover"}]
    prof = parse_ucp_profile(raw, ORIGIN)
    assert prof is not None and prof.checkout_service() is None
    out = execute_ucp_checkout(prof, amount="10.00", currency="INR", task_id="t1")
    assert out["ok"] is False
    assert "checkout" in _err(out).lower()


def test_checkout_accepts_dict_profile():
    """The loop stores the profile as a plain dict — the executor
    re-parses it instead of failing on the type."""
    prof = parse_ucp_profile(_profile_raw(), ORIGIN)
    client, _ = _mock_client_with_checkout()
    out = execute_ucp_checkout(prof.to_dict(), amount="1299.00", currency="INR", task_id="t1", client=client)
    assert out["ok"] is False  # mock charge still fails honestly
    assert "charge" in _err(out).lower() or "payment" in _err(out).lower()


def test_profile_round_trip():
    prof = parse_ucp_profile(_profile_raw(), ORIGIN)
    back = UCPProfile.from_dict(prof.to_dict())
    assert back.version == prof.version
    assert back.checkout_service().endpoint == prof.checkout_service().endpoint
    assert back.origin == ORIGIN


def test_checkout_annotations_resolve():
    """REVIEW (Task 14 audit): execute_ucp_checkout annotated its
    profile as Optional[Union[...]] but never imported Union, so
    typing.get_type_hints() raised NameError — latent breakage for
    any introspection of the function."""
    import typing

    hints = typing.get_type_hints(execute_ucp_checkout)
    assert "profile" in hints
    assert "amount" in hints
