"""Dynamic Step Narrator & Conversational Execution Voice.

Transforms low-level mechanical browser actions (e.g. click -> {"targetId": "..."})
into natural, engaging, human-like co-pilot commentary.
Uses intelligent context synthesis with a warm, conversational tone.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Optional


STORE_NAMES = {
    "amazon": "Amazon",
    "flipkart": "Flipkart",
    "croma": "Croma",
    "reliancedigital": "Reliance Digital",
    "samsung": "Samsung Store",
    "google": "Google",
    "youtube": "YouTube",
    "leetcode": "LeetCode",
    "github": "GitHub",
    "wikipedia": "Wikipedia",
    "myntra": "Myntra",
    "nykaa": "Nykaa",
    "ajio": "Ajio",
    "tatacliq": "Tata CLiQ",
    "apple": "Apple Store",
}


def _extract_site_and_query(url: str) -> tuple[str, str]:
    """Extract human-readable site name and search query from a URL."""
    if not url:
        return "the page", ""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        site_name = ""
        for key, name in STORE_NAMES.items():
            if key in host:
                site_name = name
                break
        if not site_name:
            # Fallback to stripped hostname
            parts = host.replace("www.", "").split(".")
            site_name = parts[0].capitalize() if parts else "the website"

        # Extract search query
        query_params = urllib.parse.parse_qs(parsed.query)
        q = ""
        for param in ("q", "k", "search", "query", "text", "term"):
            if param in query_params and query_params[param]:
                q = query_params[param][0].strip()
                break
        return site_name, q
    except Exception:
        return "the website", ""


def _extract_clean_product_name(text: str) -> str:
    """Extract product or subject name from title or path."""
    if not text:
        return ""
    # Remove URL slug characters
    clean = re.sub(r"[-_/]+", " ", text).strip()
    # Strip common suffixes
    clean = re.sub(r"\b(p|itm|pid|lid|dp|ref|keywords|search)\b.*", "", clean, flags=re.IGNORECASE).strip()
    return clean[:40] if clean else ""


def generate_step_narrative(
    step_num: int,
    action: dict[str, Any],
    result: dict[str, Any],
    instruction: str,
    why: Optional[str] = None,
    raw_obs: Any = None,
    previous_site: Optional[str] = None,
) -> str:
    """Generate dynamic, natural, AI-generalized step narrative for the live timeline."""
    action_type = (action.get("type") or "action").lower()
    params = action.get("params") or {}
    target = action.get("target") or {}
    target_name = (target.get("name") or target.get("targetId") or "").strip()
    ok = bool(result.get("ok"))
    evidence = result.get("evidence") or ""
    if isinstance(evidence, dict):
        evidence_str = str(evidence)
    else:
        evidence_str = str(evidence)

    target_url = params.get("url") or (raw_obs.get("url") if isinstance(raw_obs, dict) else getattr(raw_obs, "url", "")) or ""
    site_name, search_q = _extract_site_and_query(target_url)

    # Markdown link helper to preserve clickable link for Browser Inspector
    def _link(url: str, text: Optional[str] = None) -> str:
        if not url:
            return ""
        label = text or url
        return f"[{label} ↗]({url})"

    narrative = ""

    # 1. NAVIGATION ACTIONS
    if action_type == "navigate":
        nav_url = params.get("url") or target_url
        site, query = _extract_site_and_query(nav_url)
        url_link = _link(nav_url)

        if query:
            if previous_site and previous_site.lower() != site.lower():
                narrative = f"Now let's check on {site}, here we go — searching for '{query}'! {url_link}"
            elif step_num == 1:
                narrative = f"Alright, let's start by checking {site} for '{query}'... {url_link}"
            else:
                narrative = f"Heading over to {site} to look up '{query}'... {url_link}"
        elif "cart" in nav_url.lower():
            narrative = f"Navigating to the shopping cart on {site} to review items and pricing... {url_link}"
        elif "checkout" in nav_url.lower():
            narrative = f"Proceeding to checkout on {site}... {url_link}"
        else:
            if step_num == 1:
                narrative = f"Alright, opening {site} to begin our task... {url_link}"
            elif previous_site and previous_site.lower() != site.lower():
                narrative = f"Now switching over to {site} to check their options... {url_link}"
            else:
                narrative = f"Navigating to {site}... {url_link}"

    # 2. CLICK ACTIONS
    elif action_type == "click":
        lower_target = target_name.lower()
        is_popup_close = any(k in lower_target for k in ("close", "dismiss", "x", "modal", "overlay", "cancel", "popup"))
        is_search_btn = any(k in lower_target for k in ("search", "find", "submit", "go"))
        is_cart_btn = any(k in lower_target for k in ("buy", "cart", "checkout", "add to cart"))

        if not ok:
            if is_popup_close:
                narrative = f"Checking to see if there's an overlay or popup on {site_name} that needs dismissing..."
            else:
                narrative = f"Looking for '{target_name or 'the button'}' on {site_name} to proceed..."
        elif "opened new tab" in evidence_str.lower():
            dest_url_match = re.search(r"→\s*(https?://[^\s)]+)", evidence_str)
            dest_url = dest_url_match.group(1) if dest_url_match else ""
            dest_link = f" ({_link(dest_url)})" if dest_url else ""
            narrative = f"Opening product listing on {site_name} in a new tab to inspect price, specs, and offers!{dest_link}"
        elif is_popup_close:
            narrative = f"Dismissed the popup overlay on {site_name} so we can view the listings clearly."
        elif is_search_btn:
            narrative = f"Hit search on {site_name} to pull up the latest results."
        elif is_cart_btn:
            narrative = f"Clicked '{target_name}' on {site_name} to move toward the purchase flow."
        elif target_name:
            narrative = f"Selected '{target_name}' on {site_name} to view details."
        else:
            narrative = f"Clicked selected element on {site_name} to continue."

    # 3. TYPE ACTIONS
    elif action_type == "type":
        text_entered = params.get("text") or ""
        submit_flag = params.get("submit", False)
        if text_entered:
            if submit_flag:
                narrative = f"Typed '{text_entered}' into {site_name}'s search box and submitted..."
            else:
                narrative = f"Entered '{text_entered}' into the form field on {site_name}."
        else:
            narrative = f"Filled in required details on {site_name}."

    # 4. SCROLL ACTIONS
    elif action_type == "scroll":
        narrative = f"Scrolling down on {site_name} to load more results and check other sellers..."

    # 5. WAIT / OBSERVE ACTIONS
    elif action_type in ("wait", "observe"):
        narrative = f"Pausing a moment for {site_name} to finish rendering all dynamic elements..."

    # 6. FILE UPLOAD
    elif action_type == "upload_file":
        narrative = f"Uploading the requested file attachment to {site_name}..."

    # 7. SELECT OPTION
    elif action_type == "select_option":
        option = params.get("value") or params.get("label") or "option"
        narrative = f"Selected '{option}' from the dropdown on {site_name}."

    # FALLBACK
    else:
        if why:
            narrative = f"{why.strip()}"
        else:
            narrative = f"Executing {action_type} on {site_name}..."

    # Ensure step prefix
    return f"Step {step_num}: {narrative}"

