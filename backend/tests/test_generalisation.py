"""Task 8 — Generalisation: the agent works on ANY site by reading the AX
tree and the page, not by matching a hard-coded site list.

Covers:
- site hints are prose-only, advisory, and disableable;
- the planner prompt and deterministic plans do not depend on hints;
- no site-specific strings remain in planner / intent router / research
  extractor; the resolver builds no per-site search URLs;
- a plan that contradicts the live AX tree is rejected;
- generic flows: search box by role/name, banner close by accessible
  name, URL normalisation, generic comparison-site resolution, and
  domain-agnostic intent routing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.contracts import BrowserObservation, InteractiveElement
from app.grounding import build_ax_tree, resolve_ref
from app.intent_router import TaskIntent, classify_intent
from app.planner import SYSTEM_FRAME, _validate_plan, build_prompt, try_deterministic_plan
from app.privacy import classify_and_redact
from app.research.collector import normalize_url
from app.site_hints import format_site_hints, get_site_hints
from app.website_resolver import KNOWN_DOMAINS_REGISTRY, is_lookalike_or_suspicious

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = REPO_ROOT / "backend" / "app"

# A site-specific string must not appear in general code. The lookalike
# brand list in website_resolver.py is a SECURITY list (phishing defence),
# not behaviour, so that file is asserted separately below.
FORBIDDEN = (
    "flipkart", "amazon", "youtube", "leetcode", "whatsapp",
    "google flights", "google.com/travel",
    "_2KpZ6l", "twotabsearchtextbox",
    "samsung", "croma", "reliance", "bestbuy", "walmart", "ebay",
    "makemytrip",
)

# Selector-like tokens a hint must never contain (checked independently of
# the loader's own regex, so the loader cannot silently weaken).
SELECTOR_LIKE = re.compile(
    r"#\w|\[data-|\._[\w-]+|\.ytp[\w-]*|twotabsearch|data-tab=|\.s-result-item|\.a-price"
)


def _el(target_id: str, role: str, name: str, **kw) -> InteractiveElement:
    data = {
        "targetId": target_id, "role": role, "name": name, "tag": "button",
        "visible": True, "enabled": True,
        "bounds": {"x": 10, "y": 10, "width": 80, "height": 30},
    }
    data.update(kw)
    return InteractiveElement.model_validate(data)


def _obs(elements, **kw) -> BrowserObservation:
    data = {
        "tabId": 1, "url": "https://example.com/", "title": "T",
        "interactive": elements, "scroll": {}, "dpr": 1,
    }
    data.update(kw)
    return BrowserObservation.model_validate(data)


def _payload(elements, url: str = "https://example.com/", **kw):
    return classify_and_redact(_obs(elements, url=url, **kw), instruction="do the thing")[0]


def _click_plan(ref: str) -> dict:
    return {
        "goal": "click the control",
        "steps": [{
            "why": "act on the control by ref",
            "action": {
                "type": "click", "risk": "write", "verify": "clicked",
                "timeoutMs": 5000, "target": {"elementRef": ref},
            },
        }],
    }


# --- Site hints: prose-only, advisory, disableable ------------------------

def test_hints_file_is_prose_only_and_small():
    raw = json.loads((APP_DIR / "site_hints.json").read_text(encoding="utf-8"))
    domains = {k: v for k, v in raw.items() if not k.startswith("_")}
    assert 1 <= len(domains) <= 7, "hints are a handful of domains, not a site list"
    for domain, entry in domains.items():
        assert entry.get("hints"), f"hint entry for {domain} is empty"
        for text in entry["hints"]:
            assert not SELECTOR_LIKE.search(text), (
                f"hint for {domain} is selector-like (a playbook in disguise): {text!r}"
            )


def test_get_site_hints_known_and_unknown_hosts():
    assert get_site_hints("web.whatsapp.com") is not None
    assert get_site_hints("www.amazon.in") is not None
    assert get_site_hints("amazon.in") is not None
    assert get_site_hints("example.com") is None
    assert get_site_hints(None) is None
    assert get_site_hints("") is None


def test_format_site_hints_block_is_advisory():
    block = format_site_hints("web.whatsapp.com")
    assert "<site-hints" in block
    assert 'advisory="true"' in block
    assert "live accessibility tree is the truth" in block


def test_hints_disabled_yields_no_block(monkeypatch):
    monkeypatch.setattr("app.config.SITE_HINTS_ENABLED", False)
    assert format_site_hints("web.whatsapp.com") == ""
    assert format_site_hints("www.amazon.in") == ""


def test_build_prompt_includes_hints_for_known_host():
    payload = _payload([], url="https://web.whatsapp.com/")
    prompt = build_prompt("open the chat", payload, [])
    assert "<site-hints" in prompt


def test_build_prompt_has_no_hints_for_unknown_host():
    payload = _payload([], url="https://example.com/")
    prompt = build_prompt("do the thing", payload, [])
    assert "<site-hints" not in prompt


def test_build_prompt_has_no_hints_when_disabled(monkeypatch):
    monkeypatch.setattr("app.config.SITE_HINTS_ENABLED", False)
    payload = _payload([], url="https://web.whatsapp.com/")
    prompt = build_prompt("open the chat", payload, [])
    assert "<site-hints" not in prompt


def test_deterministic_plan_is_hint_independent(monkeypatch):
    """Hints are advisory, not load-bearing: the same deterministic plan
    must come out with hints on and with hints off."""
    payload = _payload([], url="about:blank")
    monkeypatch.setattr("app.config.SITE_HINTS_ENABLED", True)
    with_hints = try_deterministic_plan("Open https://example.com", payload, [])
    monkeypatch.setattr("app.config.SITE_HINTS_ENABLED", False)
    without_hints = try_deterministic_plan("Open https://example.com", payload, [])
    assert with_hints is not None and without_hints is not None
    assert with_hints.goal == without_hints.goal
    assert with_hints.steps[0].action.type == "navigate"
    assert with_hints.steps[0].action.params.url == without_hints.steps[0].action.params.url


# --- No site-specific strings in general code ------------------------------

def test_system_frame_is_general():
    assert "HOW TO ACT ON ANY SITE" in SYSTEM_FRAME
    lowered = SYSTEM_FRAME.lower()
    for bad in FORBIDDEN:
        assert bad not in lowered, f"SYSTEM_FRAME still mentions {bad!r}"


def test_no_site_specific_strings_in_planner_intent_research():
    for rel in ("planner.py", "intent_router.py", "extractors/research.py"):
        src = (APP_DIR / rel).read_text(encoding="utf-8")
        lowered = src.lower()
        for bad in FORBIDDEN:
            assert bad not in lowered, f"{rel} still mentions {bad!r}"


def test_no_site_specific_selectors_in_extension():
    """The generic element-collection path must contain no per-site
    selector, class, or data-testid. (Test fixtures may still use
    site-styled pages — detection is evidence-based, not id-based.)"""
    ext_forbidden = (
        "_2KpZ6l", "_2doB4z", "ytp-", "ytd-thumbnail",
        "twotabsearchtextbox", "field-keywords",
        "cell-frame", "chat-list-item", "conversation-compose-box",
        "centerCol", "s-result-item", "data-asin", ".a-price",
        "_30jeq3", "Nx9bqj", "priceToPay", "corePrice",
        "flipkart", "amazon", "whatsapp", "youtube", "leetcode", "croma",
    )
    for rel in (
        "lib/content.ts", "src/targeting.ts", "src/actionExecutor.ts",
        "src/companion/companionDrawer.ts",
    ):
        src = (REPO_ROOT / "extension" / rel).read_text(encoding="utf-8")
        lowered = src.lower()
        for bad in ext_forbidden:
            assert bad not in lowered, f"extension/{rel} still mentions {bad!r}"


def test_resolver_builds_no_per_site_search_urls():
    src = (APP_DIR / "website_resolver.py").read_text(encoding="utf-8")
    for builder in (
        "flipkart.com/search", "amazon.in/s?k=", "amazon.com/s?k=",
        "google.com/travel/flights", "google.com/travel/hotels",
        "croma.com/searchB", "reliancedigital.in/search",
        "bestbuy.com/site/searchpage", "walmart.com/search?q=",
    ):
        assert builder not in src, f"website_resolver.py still builds {builder!r}"
    # The registry is data, loaded from JSON — not a code table.
    assert "known_domains.json" in src
    assert len(KNOWN_DOMAINS_REGISTRY) == 19


def test_lookalike_brand_list_survives_as_security():
    """The brand list in the resolver is a phishing defence, not a
    playbook — it must stay, and it must still catch spoofs."""
    is_risk, _ = is_lookalike_or_suspicious("amaz0n.com")
    assert is_risk is True
    is_risk, _ = is_lookalike_or_suspicious("example.com")
    assert is_risk is False


# --- The live AX tree wins over any hint -----------------------------------

def test_plan_quoting_missing_ref_is_rejected():
    els = [_el("s", "searchbox", "Search"), _el("b", "button", "Go")]
    payload = _payload(els)
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        _validate_plan(_click_plan("e99"), payload=payload)


def test_plan_quoting_live_ref_passes():
    els = [_el("s", "searchbox", "Search"), _el("b", "button", "Go")]
    payload = _payload(els)
    plan = _validate_plan(_click_plan("e0"), payload=payload)
    assert plan.steps[0].action.type == "click"
    assert plan.steps[0].action.target["elementRef"] == "e0"


def test_generic_search_box_found_by_role_and_name():
    """A synthetic page the agent has never seen: the search box is found
    by role + accessible name, and a cookie banner closes by name."""
    els = [
        _el("search", "searchbox", "Search", tag="input"),
        _el("close", "button", "Close"),
        _el("dismiss", "button", "Dismiss"),
        _el("chat", "listitem", "Team chat"),
    ]
    tree = build_ax_tree(els)
    assert [n.ref for n in tree.nodes] == ["e0", "e1", "e2", "e3"]
    search = resolve_ref(tree, "e0")
    assert search is not None and search.role == "searchbox" and search.name == "Search"
    banner_close = resolve_ref(tree, "e1")
    assert banner_close is not None and banner_close.name == "Close"
    # A plan that clicks the banner-close ref validates against the tree.
    payload = _payload(els)
    plan = _validate_plan(_click_plan("e1"), payload=payload)
    assert plan.steps[0].action.target["elementRef"] == "e1"


# --- Generic flows ----------------------------------------------------------

def test_url_normalisation_strips_tracking_params():
    cleaned = normalize_url(
        "https://Example.com/p/?utm_source=google&utm_medium=cpc&ref=abc&fbclid=xyz&q=shoes"
    )
    assert "utm_source" not in cleaned
    assert "ref=" not in cleaned
    assert "fbclid" not in cleaned
    assert "q=shoes" in cleaned
    assert cleaned.startswith("https://example.com/p")


def test_comparison_sites_resolve_generically():
    """A comparison over sites the registry never heard of still works:
    domain-like tokens become required sites, bare unknown names do not."""
    from app.extractors.research import identify_required_sites, is_multi_site_instruction

    assert is_multi_site_instruction(
        "Compare price of headphones on ExampleStore.com and AnotherShop.com"
    )
    sites = identify_required_sites(
        "Compare price of headphones on ExampleStore.com and AnotherShop.com"
    )
    keys = {s["site_key"] for s in sites}
    assert keys == {"examplestore_com", "anothershop_com"}
    by_key = {s["site_key"]: s for s in sites}
    assert by_key["examplestore_com"]["default_url"] == "https://examplestore.com"
    # Bare unknown names resolve to nothing (the general planner path
    # handles them via web search instead of inventing a site).
    assert identify_required_sites("Compare FooBar and BazQux prices") == []
    # Registry sites still resolve the same way as before.
    sites = identify_required_sites(
        "Compare price of Samsung Galaxy A27 on Amazon, Flipkart, and Samsung Store"
    )
    assert {s["site_key"] for s in sites} == {"amazon", "flipkart", "samsung"}
    assert {s["site_key"]: s for s in sites}["samsung"]["name"] == "Samsung Store"


def test_intent_routing_is_domain_agnostic():
    """Sites the codebase has never heard of route to the browser just
    like sites it has — routing is by task text, not by a site list."""
    for instruction in (
        "Open ExampleStore.com and find headphones",
        "Open NeverHeardOfIt.example and search for shoes",
        "Find the cheapest laptop on ExampleStore.com",
    ):
        assert classify_intent(instruction).intent == TaskIntent.NEW_BROWSER_TASK
