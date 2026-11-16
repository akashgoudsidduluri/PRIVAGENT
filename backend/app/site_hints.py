"""Advisory site hints (Task 8).

Site playbooks used to live as code in the planner: per-site sections in
SYSTEM_FRAME that told the model how Flipkart's search box or WhatsApp
Web's chat list works. That hard-coding meant every new site needed a code
change, and the model trusted the playbook over the live page.

Hints replace playbooks with a thin, OPTIONAL, data-driven layer:

* ``site_hints.json`` holds one entry per domain: a name and a short list
  of human-readable descriptions. Nothing in a hint may be a CSS selector,
  a class name, a data-testid, or a data-tab value — the loader rejects
  those, because a hint that addresses the DOM is a playbook in disguise.
* A hint is ADVISORY ONLY. The live accessibility tree (Task 7) is the
  truth: a plan that quotes an element the tree does not have is invalid
  whether a hint named it or not, and the planner must re-observe rather
  than act on a hint that contradicts the page.
* Hints are off entirely when ``PRIVAGENT_SITE_HINTS=false``; the agent
  then runs in pure-general mode. A plan that needs a hint to work is a
  bug — the general path must produce the same plan on a synthetic page
  with hints disabled.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

# A hint must never address the DOM. Anything selector-like is rejected at
# load time so a playbook cannot sneak back in as data.
_SELECTOR_LIKE_RE = re.compile(
    r"#\w"                # #id
    r"|\[data-"           # [data-testid=...]
    r"|\._[\w-]+"         # ._2KpZ6l style hashed classes
    r"|\.ytp[\w-]*"       # .ytp-* player classes
    r"|twotabsearch"      # site-specific input id
    r"|data-tab="          # data-tab="3" style addressing
    r"|\.s-result-item"   # site result-list classes
    r"|\.a-price"          # site price classes
)

_HINTS: dict[str, dict[str, Any]] | None = None


def _load_site_hints() -> dict[str, dict[str, Any]]:
    global _HINTS
    if _HINTS is not None:
        return _HINTS
    path = Path(__file__).with_name("site_hints.json")
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    hints = {k: v for k, v in raw.items() if not k.startswith("_")}
    for domain, entry in hints.items():
        for text in entry.get("hints", []):
            if _SELECTOR_LIKE_RE.search(text):
                raise ValueError(
                    f"site hint for {domain!r} is selector-like and is "
                    f"rejected — hints must be human-readable prose: {text!r}"
                )
    _HINTS = hints
    return _HINTS


def site_hints_enabled() -> bool:
    """True unless the operator disabled hints (PRIVAGENT_SITE_HINTS=false)."""
    try:
        from . import config

        return bool(config.SITE_HINTS_ENABLED)
    except Exception:
        return True


def get_site_hints(host: str | None) -> Optional[dict[str, Any]]:
    """The hint entry for a host, or None.

    Matches by domain suffix so subdomains resolve (``web.whatsapp.com``
    matches ``whatsapp.com`` entries and vice versa); the longest matching
    domain wins.
    """
    if not host:
        return None
    host = host.lower().strip().lstrip(".")
    best: Optional[dict[str, Any]] = None
    best_len = -1
    for domain, entry in _load_site_hints().items():
        # Either direction: "web.whatsapp.com" matches the "web.whatsapp.com"
        # entry, and bare "whatsapp.com" matches it too.
        if host == domain or host.endswith("." + domain) or domain.endswith("." + host):
            if len(domain) > best_len:
                best, best_len = entry, len(domain)
    return best


def format_site_hints(host: str | None) -> str:
    """Prompt block for the current host, or "" when hints are off/unknown."""
    if not site_hints_enabled():
        return ""
    entry = get_site_hints(host)
    if not entry:
        return ""
    lines = "\n".join(f"- {text}" for text in entry.get("hints", []))
    return (
        "<site-hints trusted=\"true\" advisory=\"true\">\n"
        f"Site notes for {entry.get('name', host)} — advisory only. The live "
        "accessibility tree is the truth: never act on a hint that names an "
        "element the tree does not have, and never invent a ref for it.\n"
        f"{lines}\n"
        "</site-hints>\n\n"
    )
