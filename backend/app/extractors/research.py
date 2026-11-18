"""Structured multi-page research extractor for e-commerce, travel, and product comparison."""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Optional

from ..website_resolver import KNOWN_DOMAINS_REGISTRY, website_resolver

# Task 8: the retailer table used to be a second hard-coded copy of the
# site registry. It is now DERIVED from the data file
# (backend/app/known_domains.json, category == "retail") — adding a
# retailer is a data edit, never a code change. A registry entry may
# carry an optional "display_name" (the label comparison tables show);
# otherwise the registry name is used.
def _retailers_from_registry() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, entry in KNOWN_DOMAINS_REGISTRY.items():
        if entry.get("category") != "retail":
            continue
        regional = entry.get("regional_urls") or {}
        out[key] = {
            "key": key,
            "name": entry.get("display_name") or entry["name"],
            "domains": list(entry["allowed_domains"]),
            # Default region is IN (same as the resolver); fall back to
            # the global URL so the derived table matches the old one.
            "default_url": (
                regional.get("IN")
                or regional.get("GLOBAL")
                or f"https://{entry['primary_domain']}"
            ),
            "patterns": list(entry["patterns"]),
        }
    return out


KNOWN_RETAILERS: dict[str, dict[str, Any]] = _retailers_from_registry()

# Retail display names (data) for stripping "on <site>" tails from a
# product query and " - <site>" suffixes from a product title. Built
# from the registry, never hand-listed.
_RETAILER_DISPLAY_NAMES = "|".join(
    sorted(
        (
            re.escape(entry.get("display_name") or entry["name"])
            for entry in KNOWN_DOMAINS_REGISTRY.values()
            if entry.get("category") == "retail"
        ),
        key=len,
        reverse=True,
    )
)

# A site token after "on / across / from / at": a registry retail name
# (case-insensitive, with an optional generic word after it), any other
# capitalised name, or a domain.
_SITE_AFTER_PREP = (
    r"(?:(?i:" + _RETAILER_DISPLAY_NAMES + r")(?:\s+(?:store|shop|official|website|portal|app))?"
    r"|[A-Z][A-Za-z0-9\-]*(?:\s+(?:[A-Z][A-Za-z0-9\-]*|store|shop|official|website|portal|app))?"
    r"|[a-z][a-z0-9\-]*(?:\.[a-z]{2,})+)"
)

# Words that may follow a site name in a comparison list ("Acme Store",
# "official store"). Anything else ends the site token.
_SITE_NAME_CONTINUATIONS = {"store", "shop", "official", "website", "portal", "app"}

_COMPARE_SITE_LIST_RE = re.compile(
    # Dots stay in the capture: a site token may be a domain
    # ("examplestore.com"). Sentence-ending periods are stripped per
    # token by _site_token_words; junk tokens are filtered by resolution.
    r"\b(?:compare|comparison)\b.*?\b(?:on|across|between)\s+([^;:!?]+)",
    re.IGNORECASE,
)


def _site_token_words(token: str) -> str:
    """Leading words of a comparison-list token that look like a site name.

    "ExampleStore for under 50000" -> "ExampleStore"; "Acme Store" ->
    "Acme Store"; "examplestore.com" -> "examplestore.com".
    """
    words: list[str] = []
    for raw in token.split():
        word = raw.strip(",.")
        if not word:
            continue
        if not words:
            if word[0].isupper() or "." in word:
                words.append(word)
            else:
                break
        elif word[0].isupper() or word.lower() in _SITE_NAME_CONTINUATIONS or "." in word:
            words.append(word)
        else:
            break
    return " ".join(words)


def _generic_required_sites(
    instruction: str, seen_keys: set[str], seen_domains: set[str]
) -> list[dict[str, Any]]:
    """Required sites the user named that are NOT in the retailer registry.

    Parses the site list of a comparison phrase ("compare X on A, B and C")
    and resolves each token through the website resolver. A token becomes
    a required site when it resolves to a known registry entry, a direct
    URL, or is itself a domain (``examplestore.com``) that passes the
    lookalike check. A bare unknown name resolves to a web-search
    fallback, which is not a site — the general planner path handles it
    instead.
    """
    from ..website_resolver import is_lookalike_or_suspicious

    match = _COMPARE_SITE_LIST_RE.search(instruction or "")
    if not match:
        return []
    found: list[dict[str, Any]] = []
    for chunk in re.split(r",|\band\b", match.group(1)):
        name = _site_token_words(chunk.strip().lstrip("the ").strip())
        if not name:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not key or key in seen_keys:
            continue
        try:
            resolved = website_resolver.resolve(name)
        except Exception:
            continue
        if resolved.resolution_method in ("registry", "direct_url"):
            if not resolved.primary_domain or not resolved.canonical_url:
                continue
            if resolved.primary_domain in seen_domains:
                continue
            seen_keys.add(key)
            seen_domains.add(resolved.primary_domain)
            found.append({
                "site_key": key,
                "name": resolved.target_name,
                "domains": [resolved.primary_domain],
                "default_url": resolved.canonical_url,
            })
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", name):
            domain = name.lower()
            if domain in seen_domains:
                continue
            is_risk, _ = is_lookalike_or_suspicious(domain)
            if is_risk:
                continue
            seen_keys.add(key)
            seen_domains.add(domain)
            found.append({
                "site_key": key,
                "name": name,
                "domains": [domain],
                "default_url": f"https://{domain}",
            })
    return found


def identify_required_sites(instruction: str) -> list[dict[str, Any]]:
    """Identify required websites/stores for multi-site comparison or research."""
    if not instruction:
        return []
    inst_lower = instruction.lower()
    found: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for key, info in KNOWN_RETAILERS.items():
        matched = False
        for pat in info["patterns"]:
            if re.search(pat, inst_lower):
                matched = True
                break
        # Bare-name mention alongside another retailer: registry patterns
        # need a full name ("Acme Store") or a domain ("acme.com"),
        # but a bare name in a list ("across RetailerA, RetailerB,
        # Acme") is unambiguous once another retailer matched.
        if not matched:
            bare = info["name"].split()[0].lower()
            if re.search(r"(?:across|on|and|,)\s+(?:the\s+)?" + re.escape(bare) + r"\b", inst_lower):
                other_retailers = any(
                    k != key and any(re.search(p, inst_lower) for p in KNOWN_RETAILERS[k]["patterns"])
                    for k in KNOWN_RETAILERS
                )
                if other_retailers:
                    matched = True

        if matched and key not in seen_keys:
            seen_keys.add(key)
            found.append({
                "site_key": key,
                "name": info["name"],
                "domains": list(info["domains"]),
                "default_url": info["default_url"],
            })

    # Task 8: sites the user named that no registry covers (e.g. a
    # regional retailer, or a domain like examplestore.com) still become
    # required sites — resolved generically, not from a hard-coded list.
    seen_domains = {d for s in found for d in s["domains"]}
    found.extend(_generic_required_sites(instruction, seen_keys, seen_domains))

    return found


def is_multi_site_instruction(instruction: str) -> bool:
    """True if instruction explicitly asks for multi-site research or comparison."""
    if not instruction:
        return False
    sites = identify_required_sites(instruction)
    return len(sites) >= 2


def init_multi_site_tracker(
    instruction: str, existing_tracker: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """Initialize or update multi-site comparison tracker for multi-site tasks."""
    sites = identify_required_sites(instruction)
    if len(sites) < 2:
        return existing_tracker

    existing_sites = (existing_tracker or {}).get("required_sites", {})
    required_sites: dict[str, dict[str, Any]] = {}

    for s in sites:
        k = s["site_key"]
        if k in existing_sites:
            required_sites[k] = dict(existing_sites[k])
        else:
            required_sites[k] = {
                "site_key": k,
                "name": s["name"],
                "domains": s["domains"],
                "default_url": s["default_url"],
                "status": "pending",  # "pending", "in_progress", "completed", "unavailable"
                "data": None,
                "reason": None,
                "visited_urls": [],
            }

    return {
        "instruction": instruction,
        "required_sites": required_sites,
        "total_count": len(required_sites),
    }


def detect_site_unavailability(url: str, page_text: str, title: str) -> Optional[str]:
    """Detect if a product or listing is unavailable on the current page."""
    if not page_text and not title:
        return None
    full = f"{title}\n{page_text}".lower()
    unavail_patterns = [
        r"\bno results found\b",
        r"\bdid not match any products\b",
        r"\bwe couldn't find any matches\b",
        r"\b0 results for\b",
        r"\bcurrently unavailable\b",
        r"\bout of stock\b",
        r"\bpage not found\b",
        r"\bitem not available\b",
        r"\bproduct not available\b",
        r"\bwe couldn't find what you were looking for\b",
    ]
    for p in unavail_patterns:
        if re.search(p, full):
            return "No matching product found on this website"
    return None


def update_multi_site_tracker(
    tracker: dict[str, Any],
    url: str,
    title: str,
    page_text: str,
    collected_research: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Update required sites in tracker based on current page state and collected facts."""
    if not tracker or not tracker.get("required_sites"):
        return tracker

    coll = collected_research or {}
    domain = urllib.parse.urlparse(url).netloc.lower().replace("www.", "") if url else ""

    for site_key, site in tracker["required_sites"].items():
        # 1. Check if collected research has a fact matching this site's domains
        for d in site.get("domains", []):
            if d in coll:
                site["status"] = "completed"
                site["data"] = coll[d]
                site["reason"] = None
                break

        # 2. Check if current URL belongs to this site
        if any(d in domain for d in site.get("domains", [])):
            if url and url not in site.get("visited_urls", []):
                site.setdefault("visited_urls", []).append(url)
            # If not already completed, check if unavailable
            if site["status"] != "completed":
                unavail_reason = detect_site_unavailability(url, page_text, title)
                if unavail_reason:
                    site["status"] = "unavailable"
                    site["reason"] = unavail_reason
                elif site["status"] == "pending":
                    site["status"] = "in_progress"

    return tracker


def get_pending_sites(tracker: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sites that have not yet been completed or marked unavailable."""
    if not tracker or not tracker.get("required_sites"):
        return []
    return [s for s in tracker["required_sites"].values() if s.get("status") in ("pending", "in_progress")]


def get_completed_sites(tracker: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sites where product details and prices were successfully verified."""
    if not tracker or not tracker.get("required_sites"):
        return []
    return [s for s in tracker["required_sites"].values() if s.get("status") == "completed"]


def get_unavailable_sites(tracker: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sites confirmed to be unavailable or with no matching listing."""
    if not tracker or not tracker.get("required_sites"):
        return []
    return [s for s in tracker["required_sites"].values() if s.get("status") == "unavailable"]


def is_multi_site_task_complete(tracker: dict[str, Any]) -> bool:
    """True only when every required site is either completed or confirmed unavailable."""
    if not tracker or not tracker.get("required_sites"):
        return True
    return len(get_pending_sites(tracker)) == 0 and len(tracker["required_sites"]) > 0


def is_already_resolved_site(target_url: str, tracker: dict[str, Any]) -> bool:
    """Check if target_url belongs to a site that was already completed or marked unavailable."""
    if not tracker or not tracker.get("required_sites") or not target_url:
        return False
    try:
        target_domain = urllib.parse.urlsplit(target_url).hostname or ""
        target_domain = target_domain.lower().replace("www.", "")
    except Exception:
        return False
    if not target_domain:
        return False
    for s in tracker["required_sites"].values():
        if s.get("status") in ("completed", "unavailable"):
            if any(target_domain == d or target_domain.endswith("." + d) or d.endswith("." + target_domain) for d in s.get("domains", [])):
                return True
    return False


def extract_target_product_query(instruction: str) -> str:
    """Extract core product search query from a comparison instruction."""
    text = (instruction or "").strip()
    # Strip leading phrases
    text = re.sub(r"^(?:find|search(?:\s+for)?|look\s+up|check\s+price(?:\s+of)?|compare(?:\s+prices\s+of)?)\s+", "", text, flags=re.IGNORECASE)
    # Strip trailing "and compare prices", "and compare", "compare prices"
    text = re.sub(r"\s+(?:and\s+)?compare(?:\s+prices)?.*$", "", text, flags=re.IGNORECASE)
    # Strip "on [site]...", "across [site]..." — the site token is any
    # registry retail name, capitalised name, or domain (Task 8: no
    # hard-coded store list).
    text = re.sub(r"\s+(?:on|across|from|at)\s+" + _SITE_AFTER_PREP + r".*$", "", text)
    return text.strip()


def claims_cheapest_on_single_listing(answer: str, completed_count: int) -> bool:
    """True if answer claims 'cheapest' or 'lowest price' when fewer than 2 listings were verified."""
    if completed_count >= 2:
        return False
    ans_lower = (answer or "").lower()
    return bool(re.search(r"\b(is\s+the\s+cheapest|cheapest\s+option\s+is|lowest\s+price\s+is\s+on|cheapest\s+price\s+is\s+on)\b", ans_lower))


def format_comparison_table(tracker: dict[str, Any], instruction: str = "") -> str:
    """Format markdown comparison table distinguishing verified, unavailable, and unvisited sites."""
    if not tracker or not tracker.get("required_sites"):
        return ""

    rows = []
    completed = get_completed_sites(tracker)
    valid_prices: list[tuple[float, str, str]] = []  # (price_val, site_name, price_str)

    for s in tracker["required_sites"].values():
        status = s.get("status", "pending")
        name = s["name"]
        if status == "completed" and s.get("data"):
            d = s["data"]
            price_str = d.get("price", "N/A")
            url = d.get("url", s.get("default_url", ""))
            variant = d.get("variant", "Standard")
            rows.append(f"| **{name}** | Verified | {price_str} | {variant} | [Link]({url}) |")
            nums = re.findall(r"[\d,]+(?:\.\d+)?", price_str.replace("₹", "").replace("$", ""))
            if nums:
                try:
                    val = float(nums[0].replace(",", ""))
                    valid_prices.append((val, name, price_str))
                except ValueError:
                    pass
        elif status == "unavailable":
            reason = s.get("reason") or "No matching listing found"
            url = s.get("default_url", "")
            rows.append(f"| **{name}** | Unavailable | N/A | {reason} | [Link]({url}) |")
        else:  # pending / unvisited
            url = s.get("default_url", "")
            rows.append(f"| **{name}** | Unvisited | N/A | Not visited | [Link]({url}) |")

    table_header = "| Retailer | Status | Price | Details | Link |\n| :--- | :--- | :--- | :--- | :--- |"
    table = f"{table_header}\n" + "\n".join(rows)

    # Product images, one per verified listing that declares one —
    # the same card-with-image shape other agents show, rendered
    # above the table. The image URL comes from the product page's
    # own og:image (validated in the extractor); the workspace
    # re-validates it before emitting an <img>.
    images = []
    for s in tracker["required_sites"].values():
        if s.get("status") == "completed" and s.get("data"):
            d = s["data"]
            img = d.get("image")
            if img:
                images.append(f"![{d.get('title', s['name'])} — {d.get('price', '')}]({img})")
    images_section = ("\n\n" + "\n\n".join(images)) if images else ""

    conclusion = ""
    if len(valid_prices) >= 2:
        valid_prices.sort(key=lambda x: x[0])
        cheapest = valid_prices[0]
        second = valid_prices[1]
        diff = second[0] - cheapest[0]
        if diff > 0:
            conclusion = f"\n\n**Cheapest Option**: **{cheapest[1]}** at {cheapest[2]} (₹{diff:,.0f} lower than {second[1]} at {second[2]})."
        else:
            conclusion = f"\n\n**Cheapest Option**: **{cheapest[1]}** and **{second[1]}** are tied at {cheapest[2]}."
    elif len(valid_prices) == 1:
        # Requirement 7: Never claim the cheapest option based on only one listing
        single = valid_prices[0]
        conclusion = (
            f"\n\n**Price Finding**: **{single[1]}** has the only verified listing at {single[2]}. "
            "A comparative cheapest option cannot be determined because the other retailers were unavailable or unvisited."
        )
    else:
        conclusion = "\n\n**Summary**: No verified listings were found across the requested retailers."

    return f"### Price & Availability Comparison:{images_section}\n{table}{conclusion}"


def _valid_image_url(raw: str | None) -> str | None:
    """Accept a page-declared product image URL, or None.

    Only credential-free http(s) URLs with a hostname pass. The host
    is deliberately NOT restricted to the page host — a product image
    almost always lives on the store's image CDN, not the listing
    page's own host. The URL is only ever rendered for a product page
    the agent actually visited, and the workspace re-validates it
    with safeHttpUrl before emitting an <img>.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = urllib.parse.urlparse(raw.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc or parsed.username or parsed.password:
        return None
    return raw.strip()[:2048]


def extract_page_research(
    url: str,
    title: str,
    page_text: str,
    interactive_elements: Optional[list[dict[str, Any]]] = None,
    image_url: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Extract structured research entity (price, variant, seller, url) from visited pages.
    
    Returns None if the page does not appear to be a product, booking, or research destination.
    """
    if not url or not page_text:
        return None

    parsed_url = urllib.parse.urlparse(url)
    domain = parsed_url.netloc.lower().replace("www.", "")
    if not domain or domain in ("google.com", "bing.com", "duckduckgo.com"):
        return None

    full_text = f"{title}\n{page_text}"

    # Price pattern: ₹24,999, Rs. 24,999, INR 24999, $499, etc.
    price_pattern = r"(?:₹|Rs\.?|INR|\$)\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)"
    price_matches = re.findall(price_pattern, full_text, re.IGNORECASE)
    price_str = None
    if price_matches:
        # Take the most plausible product price (first non-zero price)
        for m in price_matches:
            cleaned = m.replace(",", "").strip()
            try:
                val = float(cleaned)
                if val >= 5:  # filter out trivial numbers like $1 or ₹1
                    symbol = "₹" if any(c in full_text[:500] for c in ("₹", "Rs", "INR")) else "$"
                    price_str = f"{symbol}{m.strip()}"
                    break
            except ValueError:
                continue

    if not price_str:
        return None

    # Clean product title: strip a trailing " - <site>" suffix, where the
    # site is a registry retail name or a domain (Task 8: no hard-coded
    # store list). A capitalised non-site word ("Silver") is kept.
    cleaned_title = title.strip()
    cleaned_title = re.sub(
        r"\s*[-|–]\s*(?:(?i:" + _RETAILER_DISPLAY_NAMES + r")|[a-z][a-z0-9\-]*(?:\.[a-z]{2,})+).*$",
        "", cleaned_title,
    ).strip()
    if len(cleaned_title) < 5:
        first_line = page_text.splitlines()[0] if page_text.splitlines() else ""
        if len(first_line) > 5:
            cleaned_title = first_line[:100].strip()

    # Variant extraction: RAM, Storage, Color, Generation
    variants = []
    storage_match = re.search(r"\b(32|64|128|256|512)\s?(?:GB|TB)\b", full_text, re.IGNORECASE)
    if storage_match:
        variants.append(storage_match.group(0).upper())

    ram_match = re.search(r"\b(4|6|8|12|16)\s?(?:GB)\s+RAM\b", full_text, re.IGNORECASE)
    if ram_match:
        variants.append(ram_match.group(0).upper())

    color_match = re.search(r"\b(?:Color|Colour):\s*([A-Za-z\s]+?)(?:,|\.|\n|$|\()", full_text, re.IGNORECASE)
    if color_match:
        color = color_match.group(1).strip()
        if len(color) < 25:
            variants.append(color)

    # Seller extraction
    seller = None
    seller_match = re.search(r"(?i)(?:Sold by|Seller:)\s*([A-Za-z0-9\s,\.\-]{3,40}?)(?:\s*(?:and Fulfilled|Free Delivery|\.|\n|$))", full_text)
    if seller_match:
        seller = seller_match.group(1).strip()
    else:
        # Task 8: the seller is the site's display name from the
        # registry, not a hard-coded branch per retailer.
        for _entry in KNOWN_DOMAINS_REGISTRY.values():
            if domain == _entry["primary_domain"] or domain in _entry.get("allowed_domains", []):
                seller = f"{_entry.get('display_name') or _entry['name']} Seller"
                break

    # Availability
    in_stock = True
    if re.search(r"(?i)\b(out of stock|currently unavailable|sold out)\b", full_text):
        in_stock = False

    return {
        "domain": domain,
        "url": url,
        "title": cleaned_title,
        "price": price_str,
        "variant": ", ".join(variants) if variants else "Standard",
        "seller": seller or domain.capitalize(),
        "in_stock": in_stock,
        # Product image from the page's og:image, validated above.
        # Absent (None) when the page declares none or it is unsafe.
        "image": _valid_image_url(image_url),
    }
