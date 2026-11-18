"""Tracker blocklist hosts (Task 12).

The enforcement copy is the declarativeNetRequest ruleset in
``extension/rules/trackers.json`` (data, not code). This module is
the backend's read-only mirror of the same 24 hosts, so the Privacy
Center can list them with a plain-language category without the
backend reaching into the extension directory (which is not shipped
in the backend Docker image).

The two copies must agree: ``test_privacy_status.py`` asserts this
list equals the hosts in ``extension/rules/trackers.json``. When a
host is added or removed, change BOTH files in the same commit.
"""
from __future__ import annotations

# (host, category) — categories match docs/ISOLATION.md.
TRACKER_HOSTS: list[tuple[str, str]] = [
    # Analytics
    ("google-analytics.com", "Analytics"),
    ("googletagmanager.com", "Analytics"),
    ("mixpanel.com", "Analytics"),
    ("cdn.amplitude.com", "Analytics"),
    ("cdn.segment.io", "Analytics"),
    ("heapanalytics.com", "Analytics"),
    ("scorecardresearch.com", "Analytics"),
    # Ads / ad-tech
    ("doubleclick.net", "Ads"),
    ("googlesyndication.com", "Ads"),
    ("adservice.google.com", "Ads"),
    ("amazon-adsystem.com", "Ads"),
    ("adsrvr.org", "Ads"),
    ("criteo.com", "Ads"),
    ("taboola.com", "Ads"),
    ("outbrain.com", "Ads"),
    ("ads.yahoo.com", "Ads"),
    # Social trackers
    ("facebook.net", "Social tracker"),
    ("connect.facebook.net", "Social tracker"),
    # Session replay / behaviour
    ("hotjar.com", "Session replay"),
    ("clarity.ms", "Session replay"),
    ("fullstory.com", "Session replay"),
    ("logrocket.com", "Session replay"),
    ("mouseflow.com", "Session replay"),
    # Fingerprinting
    ("fingerprint.com", "Fingerprinting"),
]


def tracker_hosts() -> list[dict[str, str]]:
    """JSON-safe list: [{"host": ..., "category": ...}, ...]."""
    return [{"host": host, "category": category} for host, category in TRACKER_HOSTS]
