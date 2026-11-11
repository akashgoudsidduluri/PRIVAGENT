"""Structured multi-page research extractor for e-commerce, travel, and product comparison."""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Optional


KNOWN_RETAILERS: dict[str, dict[str, Any]] = {
    "amazon": {
        "key": "amazon",
        "name": "Amazon",
        "domains": ["amazon.in", "amazon.com"],
        "default_url": "https://www.amazon.in",
        "patterns": [r"\bamazon(?:\.in|\.com)?\b"],
    },
    "flipkart": {
        "key": "flipkart",
        "name": "Flipkart",
        "domains": ["flipkart.com"],
        "default_url": "https://www.flipkart.com",
        "patterns": [r"\bflipkart(?:\.com)?\b"],
    },
    "samsung": {
        "key": "samsung",
        "name": "Samsung Store",
        "domains": ["samsung.com"],
        "default_url": "https://www.samsung.com/in/",
        "patterns": [
            r"\bsamsung(?:'s)?(?:\s+official)?\s+(?:store|shop|website|portal)\b",
            r"\bsamsung\.com\b",
        ],
    },
    "croma": {
        "key": "croma",
        "name": "Croma",
        "domains": ["croma.com"],
        "default_url": "https://www.croma.com",
        "patterns": [r"\bcroma(?:\.com)?\b"],
    },
    "reliance_digital": {
        "key": "reliance_digital",
        "name": "Reliance Digital",
        "domains": ["reliancedigital.in"],
        "default_url": "https://www.reliancedigital.in",
        "patterns": [r"\breliance\s+digital\b", r"\breliancedigital(?:\.in)?\b"],
    },
    "best_buy": {
        "key": "best_buy",
        "name": "Best Buy",
        "domains": ["bestbuy.com"],
        "default_url": "https://www.bestbuy.com",
        "patterns": [r"\bbest\s*buy(?:\.com)?\b"],
    },
    "walmart": {
        "key": "walmart",
        "name": "Walmart",
        "domains": ["walmart.com"],
        "default_url": "https://www.walmart.com",
        "patterns": [r"\bwalmart(?:\.com)?\b"],
    },
    "ebay": {
        "key": "ebay",
        "name": "eBay",
        "domains": ["ebay.com"],
        "default_url": "https://www.ebay.com",
        "patterns": [r"\bebay(?:\.com)?\b"],
    },
}


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
        # Special check for Samsung when listed alongside other retailers
        if not matched and key == "samsung":
            # e.g., "across Amazon, Flipkart, Samsung" or "Amazon, Flipkart and Samsung"
            if re.search(r"(?:across|on|and|,)\s+(?:the\s+)?samsung\b", inst_lower):
                other_retailers = any(
                    k != "samsung" and any(re.search(p, inst_lower) for p in KNOWN_RETAILERS[k]["patterns"])
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
    # Strip "on [store]...", "across [store]..."
    text = re.sub(r"\s+(?:on|across|from|at)\s+(?:amazon|flipkart|samsung|croma|best\s*buy|walmart|ebay|official\s+store).*$", "", text, flags=re.IGNORECASE)
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

    return f"### Price & Availability Comparison:\n{table}{conclusion}"


def extract_page_research(
    url: str,
    title: str,
    page_text: str,
    interactive_elements: Optional[list[dict[str, Any]]] = None,
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

    # Clean product title
    cleaned_title = title.strip()
    cleaned_title = re.sub(r"(?i)\s*[-|–]\s*(Amazon\.in|Flipkart|Croma|Reliance Digital|eBay|Best Buy).*$", "", cleaned_title).strip()
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
    elif "amazon" in domain:
        seller = "Amazon Seller"
    elif "flipkart" in domain:
        seller = "Flipkart Seller"

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
    }
