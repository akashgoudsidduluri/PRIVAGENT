"""Task 10 — Tracker / fingerprint hygiene.

Covers: the DNR blocklist is valid JSON with unique ids, every rule
blocks, the 12 original hosts are still there plus the 12 Task 10
additions; the agent-profile launch scripts carry the privacy flags;
and the extension exposes no extra navigator data to pages (the only
navigator read is the macOS platform check in actionExecutor).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RULES_PATH = REPO_ROOT / "extension" / "rules" / "trackers.json"

ORIGINAL_HOSTS = {
    "||google-analytics.com", "||googletagmanager.com", "||doubleclick.net",
    "||googlesyndication.com", "||facebook.net", "||connect.facebook.net",
    "||hotjar.com", "||mixpanel.com", "||cdn.amplitude.com",
    "||cdn.segment.io", "||scorecardresearch.com", "||ads.yahoo.com",
}

# Task 10 additions, by category.
ADDED_HOSTS = {
    # Ads / ad-tech
    "||adservice.google.com", "||amazon-adsystem.com", "||adsrvr.org",
    "||criteo.com", "||taboola.com", "||outbrain.com",
    # Session replay / behaviour analytics
    "||clarity.ms", "||fullstory.com", "||logrocket.com", "||mouseflow.com",
    "||heapanalytics.com",
    # Fingerprinting
    "||fingerprint.com",
}

LAUNCH_FLAGS = {
    "--block-third-party-cookies",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-domain-reliability",
    "--no-pings",
}


def _rules():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def test_rules_are_valid_json_with_unique_ids():
    rules = _rules()
    assert isinstance(rules, list) and len(rules) == 24
    ids = [r["id"] for r in rules]
    assert len(set(ids)) == len(ids) == 24
    assert sorted(ids) == list(range(1, 25))


def test_every_rule_blocks_a_named_host():
    for rule in _rules():
        assert rule["action"] == {"type": "block"}
        cond = rule["condition"]
        assert cond["urlFilter"].startswith("||"), cond
        assert cond["resourceTypes"], cond
        assert rule["priority"] == 1


def test_original_hosts_still_blocked_and_additions_present():
    hosts = {r["condition"]["urlFilter"] for r in _rules()}
    assert ORIGINAL_HOSTS <= hosts, f"missing originals: {ORIGINAL_HOSTS - hosts}"
    assert ADDED_HOSTS <= hosts, f"missing additions: {ADDED_HOSTS - hosts}"


def test_launch_scripts_carry_privacy_flags():
    for script in ("launch-agent-chrome.sh", "launch-agent-chrome.bat"):
        text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        for flag in LAUNCH_FLAGS:
            assert flag in text, f"{script} is missing {flag}"
        # The isolated-profile launch must survive: the user-data-dir
        # flag is the whole point of these scripts.
        assert "--user-data-dir" in text


def test_extension_exposes_no_extra_navigator_data():
    # The content script must not add fingerprinting surface: the only
    # navigator read in the extension source is the macOS platform
    # check in actionExecutor (keyboard-shortcut difference) — it reads
    # what any page could already read itself.
    hits: list[str] = []
    for path in sorted((REPO_ROOT / "extension" / "src").rglob("*.ts")) + \
            sorted((REPO_ROOT / "extension" / "lib").rglob("*.ts")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"navigator\.(\w+)", text):
            hits.append(f"{path.name}:{match.group(0)}")
    assert hits, "expected at least the actionExecutor platform read"
    for hit in hits:
        assert hit.endswith(("navigator.platform", "navigator.userAgent")), (
            f"unexpected navigator read (new fingerprinting surface?): {hit}"
        )
