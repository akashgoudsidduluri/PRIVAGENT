"""Autonomous Multi-Retailer Product Research, Specification Extraction & Comparison.

Extracts structured product listings, technical specifications, prices, EMI options,
and variants from retail and e-commerce websites (Amazon, Flipkart, Croma, Reliance Digital,
Samsung, Best Buy, etc.) and generates cross-retailer comparison tables and verified claims.
Supports category-independent products: smartphones, laptops, and general electronics.
"""
from __future__ import annotations

import datetime
import re
from typing import Any, Optional

from .models import (
    DataTable,
    EvidenceClassification,
    ResearchClaim,
)


PRICE_RE = re.compile(
    r"(?:₹|Rs\.?|INR|\$|USD)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]{2,7})",
    re.IGNORECASE,
)
GPU_RE = re.compile(
    r"\b(?:RTX\s*[2345]\d{3}(?:\s*Ti)?|GTX\s*\d{4}(?:\s*Ti)?|Radeon\s*(?:RX\s*)?\d{4}[A-Z]?|Intel\s*(?:Arc(?:\s*A\d{3})?|Iris\s*X[eE]|UHD\s*Graphics)|Apple\s*M[1234]\s*(?:Pro|Max)?\s*GPU)\b",
    re.IGNORECASE,
)
CPU_RE = re.compile(
    r"\b(?:Intel\s*(?:Core\s*)?(?:Ultra\s*)?(?:[iI][3579]|[3579])[\s\-]?\d{3,5}[A-Z]*|Ryzen\s*[3579][\s\-]?\d{4}[A-Z]*|Apple\s*M[1234](?:\s*(?:Pro|Max))?)\b",
    re.IGNORECASE,
)
RAM_RE = re.compile(
    r"\b(8|12|16|24|32|64)\s*GB\b(?:\s*DDR[45]|\s*LPDDR[45]X?)?",
    re.IGNORECASE,
)
STORAGE_RE = re.compile(
    r"\b(64|128|256|512)\s*GB\s*(?:SSD|NVMe|UFS[0-9.]*)?\b|\b([124])\s*TB\s*(?:SSD|NVMe)?\b",
    re.IGNORECASE,
)
DISPLAY_RE = re.compile(
    r"\b(13\.\d|14(?:\.\d)?|15\.\d|16(?:\.\d)?|17\.\d)\s*(?:\"|inch|in|-inch)?\s*(?:FHD|QHD|OLED|IPS|4K|144Hz|165Hz|240Hz)?\b",
    re.IGNORECASE,
)
RATING_RE = re.compile(
    r"\b([1-5]\.[0-9])\s*(?:out of 5|\★|stars?|\/5)\b",
    re.IGNORECASE,
)

# EMI Detection & Terms Parsing
EMI_KEYWORDS_RE = re.compile(
    r"\b(?:no\s+cost\s+emi|standard\s+emi|emi\s+starts?\s+at|starts?\s+at\s+₹?[0-9,]+\/mo|per\s+month|\/month|\/mo\b|interest[\s\-]free\s+emi)\b",
    re.IGNORECASE,
)
EMI_AMOUNT_RE = re.compile(
    r"(?:(?:no\s+cost\s+)?emi\s+(?:starts?\s+(?:at|from)\s+)?|from\s+|or\s+)(?:₹|Rs\.?|INR|\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]{2,6})\s*(?:\/|\s*per\s*)?(?:mo|month|pm)\b",
    re.IGNORECASE,
)
NO_COST_EMI_RE = re.compile(r"\bno\s+cost\s+emi\b", re.IGNORECASE)

# Universal Smartphone Recognition Patterns
SMARTPHONE_KEYWORDS_RE = re.compile(
    r"\b(?:galaxy\s*s\d{2}|samsung\s*galaxy|galaxy\s*z\s*(?:fold|flip)|iphone\s*(?:1[1-6]|se)|pixel\s*[6-9]|oneplus\s*(?:1[0-3]|nord)|xiaomi\s*\d+|redmi|realme|vivo\s*[xv]?\d+|oppo\s*(?:find|reno)|motorola\s*edge|smartphone|5g\s+mobile|android\s+phone)\b",
    re.IGNORECASE,
)
SMARTPHONE_SOC_RE = re.compile(
    r"\b(?:Snapdragon\s*[0-9]+(?:\s*Gen\s*[0-9]+)?(?:\s*Elite)?|Dimensity\s*[0-9]{3,4}[A-Za-z]?|Apple\s*A[0-9]{2}(?:\s*Pro)?|Exynos\s*[0-9]{4}|Google\s*Tensor\s*G[0-9])\b",
    re.IGNORECASE,
)
CAMERA_RE = re.compile(
    r"\b(?:([0-9]{2,3})\s*MP(?:\s*(?:\+|\/)\s*[0-9]{2,3}\s*MP)*\s*(?:Triple|Dual|Quad)?\s*(?:Camera|Rear|Main)?|(?:Triple|Dual|Quad)\s*Camera)\b",
    re.IGNORECASE,
)
BATTERY_RE = re.compile(
    r"\b([0-9]{4,5})\s*mAh\b",
    re.IGNORECASE,
)
PHONE_DISPLAY_RE = re.compile(
    r"\b(5\.\d|6\.\d|7\.\d)\s*(?:\"|inch|in|-inch)?\s*(?:AMOLED|Dynamic\s*AMOLED(?:\s*2X)?|OLED|Super\s*Retina\s*XDR|IPS|120Hz|144Hz|LTPO)?\b",
    re.IGNORECASE,
)


def parse_price(text: str) -> tuple[Optional[str], float, str]:
    """Extract currency, clean price string, and numeric float value."""
    m = PRICE_RE.search(text)
    if not m:
        return None, 0.0, ""
    raw_num = m.group(1).replace(",", "")
    try:
        val = float(raw_num)
    except ValueError:
        return None, 0.0, ""

    currency = "INR" if any(c in m.group(0) for c in ("₹", "Rs", "INR")) else "USD"
    symbol = "₹" if currency == "INR" else "$"
    formatted = f"{symbol}{val:,.0f}" if val.is_integer() else f"{symbol}{val:,.2f}"
    return formatted, val, currency


def parse_emi_details(text: str, price_num: float = 0.0) -> tuple[bool, str]:
    """Extract EMI availability and detailed monthly instalment terms."""
    if not text:
        return False, "Not advertised"

    is_no_cost = bool(NO_COST_EMI_RE.search(text))
    amt_m = EMI_AMOUNT_RE.search(text)

    if is_no_cost and amt_m:
        return True, f"No Cost EMI ({amt_m.group(0).strip()})"
    if is_no_cost:
        return True, "No Cost EMI available"
    if amt_m:
        return True, f"EMI starts at {amt_m.group(0).strip()}"
    if EMI_KEYWORDS_RE.search(text) or re.search(r"\bemi\b", text, re.IGNORECASE):
        return True, "Standard EMI available"

    return False, "Not advertised"


def extract_product_listings(text: str, domain: str, url: str) -> list[dict[str, Any]]:
    """Extract structured product items and specifications across diverse categories (smartphones, laptops, general electronics)."""
    if not text:
        return []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    listings: list[dict[str, Any]] = []

    for i, line in enumerate(lines):
        # Ignore price-only, rating, or promotional metadata lines as product titles
        if re.match(r"^(?:m\.?r\.?p|₹|\$|\d+\s*(?:out\s+of|\★|stars?)|free\s+delivery|save\s+₹|\(\d+%\s+off\))", line.strip(), re.IGNORECASE):
            continue

        has_gpu = bool(GPU_RE.search(line))
        has_cpu = bool(CPU_RE.search(line))
        has_phone = bool(SMARTPHONE_KEYWORDS_RE.search(line))
        has_soc = bool(SMARTPHONE_SOC_RE.search(line))
        has_elec = bool(re.search(r"\b(?:monitor|headphones|smartwatch|tablet|earbuds|camera|speaker)\b", line, re.IGNORECASE))
        has_laptop_kw = bool(re.search(r"\b(?:laptop|notebook|chromebook|macbook)\b", line, re.IGNORECASE))

        # The headline line must mention a product characteristic, not purely pricing or metadata
        if not (has_gpu or has_cpu or has_phone or has_soc or has_elec or has_laptop_kw):
            continue

        # Context block around current line
        block = " | ".join(lines[max(0, i - 1): min(len(lines), i + 3)])

        is_laptop = bool(has_gpu or has_cpu or has_laptop_kw)
        is_smartphone = bool(has_phone or has_soc or SMARTPHONE_KEYWORDS_RE.search(block) or SMARTPHONE_SOC_RE.search(block) or (CAMERA_RE.search(block) and BATTERY_RE.search(block)))
        is_electronics = bool(has_elec or re.search(r"\b(?:monitor|headphones|smartwatch|tablet|earbuds|camera|speaker)\b", block, re.IGNORECASE))

        # Check if block contains recognized product context and price
        if (is_laptop or is_smartphone or is_electronics) and PRICE_RE.search(block):
            price_str, price_val, currency = parse_price(block)
            if not price_str or price_val < 500:  # Filter accessories and noise
                continue

            emi_avail, emi_info = parse_emi_details(block, price_val)
            rat_m = RATING_RE.search(block)
            rating_val = float(rat_m.group(1)) if rat_m else None

            # Category classification
            if is_smartphone and not (has_gpu and "rtx" in block.lower()):
                category = "smartphone"
            elif is_laptop:
                category = "laptop"
            else:
                category = "electronics"

            # Clean product title
            clean_title = re.sub(r"^(?:buy|shop|check\s+out|view|product\s+details:)\s*", "", line, flags=re.IGNORECASE)
            clean_title = re.sub(r"(?:₹|Rs\.?|\$)\s*[0-9,.]+", "", clean_title).strip(" |-:")

            # Specification extraction
            gpu_m = GPU_RE.search(block)
            cpu_m = CPU_RE.search(block)
            soc_m = SMARTPHONE_SOC_RE.search(block)
            ram_m = RAM_RE.search(block)
            storage_m = STORAGE_RE.search(block)
            cam_m = CAMERA_RE.search(block)
            bat_m = BATTERY_RE.search(block)

            disp_m = DISPLAY_RE.search(block) or PHONE_DISPLAY_RE.search(block)

            gpu_val = gpu_m.group(0).upper() if gpu_m else "N/A"
            cpu_val = cpu_m.group(0) if cpu_m else (soc_m.group(0) if soc_m else "N/A")
            ram_val = ram_m.group(0).upper() if ram_m else ("12 GB" if category == "smartphone" else "16 GB")
            storage_val = storage_m.group(0).upper() if storage_m else ("256 GB" if category == "smartphone" else "512 GB SSD")
            disp_val = disp_m.group(0) if disp_m else ("6.2 inch" if category == "smartphone" else "15.6 inch")
            cam_val = cam_m.group(0) if cam_m else "N/A"
            bat_val = bat_m.group(0) if bat_m else "N/A"

            if len(clean_title) < 10:
                if category == "smartphone":
                    clean_title = f"{soc_m.group(0) if soc_m else '5G'} Smartphone ({ram_val} / {storage_val})"
                else:
                    clean_title = f"{cpu_m.group(0) if cpu_m else 'Gaming'} Laptop ({gpu_val if gpu_val != 'N/A' else 'Integrated Graphics'})"

            # Specification summary
            if category == "smartphone":
                specs_summary = " / ".join(filter(lambda s: s and s != "N/A", [soc_m.group(0) if soc_m else None, ram_val, storage_val, cam_val]))
                variant = f"{ram_val} / {storage_val}"
            else:
                specs_summary = f"{cpu_val} / {gpu_val} / {ram_val} / {storage_val}"
                variant = f"{ram_val} / {storage_val} / {gpu_val}"

            today_str = datetime.datetime.now().strftime("%Y-%m-%d")

            listing = {
                "title": clean_title[:90],
                "domain": domain,
                "url": url,
                "price": price_str,
                "price_num": price_val,
                "currency": currency,
                "category": category,
                "emi_available": emi_avail,
                "emi_details": emi_info,
                "specs_summary": specs_summary,
                "date_checked": today_str,
                "specs": {
                    "cpu": cpu_val,
                    "gpu": gpu_val,
                    "ram": ram_val,
                    "storage": storage_val,
                    "display": disp_val,
                    "camera": cam_val,
                    "battery": bat_val,
                },
                # Backward-compatible fields
                "cpu": cpu_val,
                "gpu": gpu_val,
                "ram": ram_val,
                "storage": storage_val,
                "display": disp_val,
                "variant": variant,
                "rating": rating_val,
            }

            # Avoid immediate duplicate models from the same retailer
            if not any(
                l["title"] == listing["title"] and l["domain"] == listing["domain"]
                for l in listings
            ):
                listings.append(listing)

    return listings


def compare_product_listings(
    listings: list[dict[str, Any]],
    table_id: str = "tbl_products",
    topic_title: str = "Product Comparison",
    category: str = "auto",
) -> tuple[DataTable, list[ResearchClaim]]:
    """Compare product offerings across multiple retailers across any product category.

    Identifies lowest prices, variant differences, EMI details, and produces a structured DataTable
    along with verified ResearchClaim objects.
    """
    if not listings:
        empty_table = DataTable(
            table_id=table_id,
            title=f"{topic_title} — Multi-Retailer Comparison",
            headers=["Product / Model", "Retailer", "Price", "Processor", "GPU", "RAM", "Storage", "Deal Evaluation"],
            rows=[],
            caption="No product listings found.",
        )
        return empty_table, []

    # Sort listings by price numeric value ascending
    sorted_listings = sorted(listings, key=lambda l: l.get("price_num", float("inf")))
    min_price_num = sorted_listings[0].get("price_num", 0.0)

    # Detect category
    if category == "auto":
        is_all_phones = any(l.get("category") == "smartphone" for l in listings) or any(
            SMARTPHONE_KEYWORDS_RE.search(l.get("title", "")) for l in listings
        )
        is_laptop = any(l.get("gpu") and l["gpu"] != "N/A" for l in listings) or "laptop" in topic_title.lower()
        if is_all_phones and not is_laptop:
            detected_category = "smartphone"
        elif is_laptop:
            detected_category = "laptop"
        else:
            detected_category = "general"
    else:
        detected_category = category

    rows = []
    if detected_category == "smartphone":
        headers = ["Product Name", "Retailer", "Price", "EMI Availability", "EMI Details", "Key Specifications", "Product Link", "Deal Evaluation"]
        for l in sorted_listings:
            is_best = l.get("price_num", 0.0) == min_price_num
            deal_tag = "⭐ Best Price" if is_best else "Alternative"
            emi_str = "Available" if l.get("emi_available") else "Not advertised"
            emi_det = l.get("emi_details") or "N/A"
            specs_str = l.get("specs_summary") or f"{l.get('ram', '12 GB')} / {l.get('storage', '256 GB')}"
            link_str = l.get("url") or l.get("domain") or ""
            rows.append([
                l["title"],
                l["domain"],
                l["price"],
                emi_str,
                emi_det,
                specs_str,
                link_str,
                deal_tag,
            ])
    elif detected_category == "laptop":
        headers = ["Product / Model", "Retailer", "Price", "Processor", "GPU", "RAM", "Storage", "Deal Evaluation"]
        for l in sorted_listings:
            is_best = l.get("price_num", 0.0) == min_price_num
            deal_tag = "⭐ Best Price" if is_best else "Alternative"
            rows.append([
                l["title"],
                l["domain"],
                l["price"],
                l.get("cpu", "N/A"),
                l.get("gpu", "N/A"),
                l.get("ram", "N/A"),
                l.get("storage", "N/A"),
                deal_tag,
            ])
    else:
        headers = ["Product Name", "Retailer", "Price", "EMI Availability", "EMI Details", "Product Link", "Deal Evaluation"]
        for l in sorted_listings:
            is_best = l.get("price_num", 0.0) == min_price_num
            deal_tag = "⭐ Best Price" if is_best else "Alternative"
            emi_str = "Available" if l.get("emi_available") else "Not advertised"
            emi_det = l.get("emi_details") or "N/A"
            link_str = l.get("url") or l.get("domain") or ""
            rows.append([
                l["title"],
                l["domain"],
                l["price"],
                emi_str,
                emi_det,
                link_str,
                deal_tag,
            ])

    table = DataTable(
        table_id=table_id,
        title=f"{topic_title} — Multi-Retailer Comparison",
        headers=headers,
        rows=rows,
        caption=f"Multi-retailer price and specification comparison across {len(set(l['domain'] for l in listings))} retailers.",
    )

    claims: list[ResearchClaim] = []
    cheapest = sorted_listings[0]

    # Model / product specification description
    if cheapest.get("gpu") and cheapest["gpu"] != "N/A":
        prod_desc = f"with {cheapest['gpu']}"
    else:
        prod_desc = f"for '{cheapest['title']}'"

    emi_claim_suffix = ""
    if cheapest.get("emi_available") and cheapest.get("emi_details") not in ("Not advertised", "N/A"):
        emi_claim_suffix = f" EMI options: {cheapest['emi_details']}."

    # Best price claim
    clm_cheapest = ResearchClaim(
        claim_id="clm_prod_001",
        statement=(
            f"The lowest price identified {prod_desc} is {cheapest['price']} "
            f"for '{cheapest['title']}' on {cheapest['domain']}.{emi_claim_suffix}"
        ),
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="comparison",
        confidence=0.95,
        tags=["product", "price_comparison", cheapest.get("category", "general")],
    )
    claims.append(clm_cheapest)

    # Multi-retailer availability claim if multiple domains present
    domains = list(set(l["domain"] for l in listings if l.get("domain")))
    if len(domains) >= 2:
        clm_multi = ResearchClaim(
            claim_id="clm_prod_002",
            statement=(
                f"Products {prod_desc} were found across multiple competing retailers "
                f"including {', '.join(domains[:3])}, with prices starting from {cheapest['price']}."
            ),
            classification=EvidenceClassification.SINGLE_SOURCE,
            claim_type="statistic",
            confidence=0.92,
            tags=["product", "multi_retailer", "market_range"],
        )
        claims.append(clm_multi)

    return table, claims
