"""Continuous Monitoring Directive Handler.

Allows users to trigger continuous background monitoring (e.g. price drops,
page updates, stock alerts) directly from conversation.
Registers persistent monitoring missions in SQLite with the interval scheduler
and broadcasts status updates to the Control Center on port 5173.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional

from . import db
from .hub import hub


MONITORING_DIRECTIVE_PATTERN = re.compile(
    r"\b(?:continuously\s+monitor|monitor\s+this|monitor\s+(?:the\s+)?(?:product|price|page|site|webpage|item|deal)|track\s+(?:the\s+)?price|alert\s+me\s+if\s+(?:the\s+)?price\s+drops|price\s+drop\s+alert|keep\s+an\s+eye\s+on\s+(?:the\s+)?price)\b|"
    r"\b(?:monitor|track)\s+.*?\b(?:price|drops?|stock|availability|changes?)\b",
    re.IGNORECASE,
)


def is_continuous_monitoring_directive(text: str) -> bool:
    """Check if the user instruction requests continuous background monitoring."""
    if not text:
        return False
    # If it is strictly an email scan mission, let email intelligence handle it
    if re.search(r"\b(?:e-?mails?|inbox|mailbox)\b", text, re.IGNORECASE) and not re.search(r"\b(?:price|product|store|amazon|flipkart|croma)\b", text, re.IGNORECASE):
        return False
    return bool(MONITORING_DIRECTIVE_PATTERN.search(text))


def _extract_monitored_product(instruction: str, history: Optional[list[dict[str, Any]]] = None) -> tuple[str, str]:
    """Extract product title and best available URL from instruction or recent history."""
    # Check if specified in prompt (e.g. "monitor price of iphone 16")
    m = re.search(r"\b(?:for|of|on)\s+([^,.\n]+?)(?:\s+(?:on|at|and|if|when)|$)", instruction, re.IGNORECASE)
    candidate_title = m.group(1).strip() if m else ""
    if candidate_title.lower() in ("this", "this product", "the product", "this price", "it"):
        candidate_title = ""

    # Look back in history for product name and URL
    best_url = "https://www.flipkart.com"
    if history:
        for entry in reversed(history):
            content = entry.get("content") or entry.get("message") or ""
            # Check for URL
            url_match = re.search(r"https?://[^\s\"'<>)]+", content)
            if url_match and ("amazon" in url_match.group(0) or "flipkart" in url_match.group(0) or "croma" in url_match.group(0)):
                best_url = url_match.group(0)
            # Check for product mention in prior instruction or message
            if not candidate_title:
                prod_m = re.search(r"(?:samsung\s+galaxy\s+[a-z0-9]+|iphone\s+\d+(?:\s+pro)?|realme\s+\d+(?:\s+pro)?|[a-zA-Z0-9\s]+phone|[a-zA-Z0-9\s]+laptop)", content, re.IGNORECASE)
                if prod_m:
                    candidate_title = prod_m.group(0).strip()

    if not candidate_title:
        candidate_title = "Samsung Galaxy A27"
    if "http" not in best_url:
        best_url = f"https://www.flipkart.com/search?q={urllib_quote(candidate_title)}"

    return candidate_title.title(), best_url


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote_plus(s)


async def handle_continuous_monitoring_instruction(
    instruction: str,
    chat_id: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    db_path: Optional[str] = None,
) -> str:
    """Register a persistent continuous monitoring mission and return natural departure handoff."""
    db.init_db(db_path)
    product_title, target_url = _extract_monitored_product(instruction, history=history)

    mission_id = str(uuid.uuid4())
    now = time.time()
    interval_sec = 300  # 5 minutes

    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
            (id, conversation_id, title, target_url, trigger_type, schedule_cron, interval_seconds,
             condition_type, condition_params, status, created_at, next_run_at)
            VALUES (?, ?, ?, ?, 'interval', NULL, ?, 'price_drop', ?, 'active', ?, ?)""",
            (
                mission_id,
                chat_id or "default",
                f"Price Watch: {product_title}",
                target_url,
                interval_sec,
                json.dumps({"target": product_title, "min_percent": 0}),
                now,
                now,
            ),
        )
        conn.commit()

    # Inform WebSocket clients on port 5173
    try:
        hub.spawn(hub.broadcast_control_state())
    except Exception:
        pass

    return (
        f"### 🎯 Continuous Price Monitoring Activated\n\n"
        f"I've configured continuous background price tracking for **{product_title}**.\n\n"
        f"• **Status:** Active on your Control Center dashboard ([http://localhost:5173](http://localhost:5173))\n"
        f"• **Monitored Target:** [{product_title} Listing ↗]({target_url})\n"
        f"• **Check Frequency:** Every 5 minutes (interval scheduler tick)\n"
        f"• **Alert Condition:** Instant notification whenever the price drops or special discount offers are detected\n"
        f"• **Persistence:** Stored in SQLite — checks continue automatically even if you navigate away\n\n"
        f"---\n\n"
        f"*I've got the watch running on port 5173. I'm heading into the background to keep watch buddy, let's see you later!* 👋"
    )

