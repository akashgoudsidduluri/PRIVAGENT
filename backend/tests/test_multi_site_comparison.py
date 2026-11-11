"""Regression tests for multi-site research and comparison tasks.

Requirements verified:
1. Track required websites and their completion status for multi-site research tasks.
2. Do not mark a comparison task completed until all requested websites have been visited or explicitly marked unavailable.
3. Preserve extracted information from previously visited websites.
4. Prevent unnecessary navigation back to already visited websites.
5. Require a final comparison using the collected results.
6. Clearly distinguish verified results, unavailable websites and unvisited websites.
7. Never claim the cheapest option based on only one listing.
8. Provider failure during comparison: resume without repeating completed visits.
"""
from __future__ import annotations

import asyncio
import pytest
from typing import Any

from app import agentLoop, db
from app.contracts import BrowserObservation, BrowserAction, TaskPlan
from app.extractors.research import (
    identify_required_sites,
    init_multi_site_tracker,
    update_multi_site_tracker,
    is_multi_site_task_complete,
    get_pending_sites,
    get_completed_sites,
    get_unavailable_sites,
    format_comparison_table,
    claims_cheapest_on_single_listing,
    is_already_resolved_site,
)
from app.planner import plan_from_observation, _validate_plan
from app.privacy import classify_and_redact


def _payload(url: str, title: str = "Page", page_text: str = "", interactive: list[dict] | None = None, instruction: str = "compare"):
    obs = BrowserObservation.model_validate({
        "tabId": 1,
        "url": url,
        "title": title,
        "page_text": page_text,
        "interactive": interactive or [],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1.0,
    })
    payload, _ = classify_and_redact(obs, instruction=instruction)
    return payload


def _completion(answer: str = "Done") -> dict[str, Any]:
    return {"goal": "compare", "steps": [], "answer": answer}


COMPARISON_INSTRUCTION = (
    "Find the Samsung Galaxy A27 at the lowest price across Amazon, Flipkart and Samsung's official store. "
    "Compare prices for the same variant, remember prices from each site, and give me a comparison table."
)


# --- 1. One of three websites visited: task must remain incomplete -----------

def test_one_of_three_websites_visited_remains_incomplete():
    tracker = init_multi_site_tracker(COMPARISON_INSTRUCTION)
    assert tracker is not None
    assert len(tracker["required_sites"]) == 3
    assert set(tracker["required_sites"].keys()) == {"amazon", "flipkart", "samsung"}

    # Simulate Amazon completed with extracted price ₹30,700
    tracker["required_sites"]["amazon"]["status"] = "completed"
    tracker["required_sites"]["amazon"]["data"] = {
        "domain": "amazon.in",
        "url": "https://www.amazon.in/samsung-a27",
        "title": "Samsung Galaxy A27 5G",
        "price": "₹30,700",
        "variant": "128GB, 8GB RAM",
        "seller": "Amazon Seller",
    }
    # Flipkart and Samsung remain pending
    assert tracker["required_sites"]["flipkart"]["status"] == "pending"
    assert tracker["required_sites"]["samsung"]["status"] == "pending"

    wm = {
        "multi_site_research": tracker,
        "collected_research": {"amazon.in": tracker["required_sites"]["amazon"]["data"]},
    }

    # Model prematurely emits steps: [] claiming success based only on Amazon
    raw_plan = _completion("Samsung Galaxy A27 is available on Amazon for ₹30,700.")
    plan = _validate_plan(
        raw_plan,
        payload=_payload("https://www.amazon.in/samsung-a27", title="Samsung Galaxy A27", instruction=COMPARISON_INSTRUCTION),
        history=[{"action": {"type": "navigate", "params": {"url": "https://www.amazon.in"}}, "result_ok": True}],
        instruction=COMPARISON_INSTRUCTION,
        working_memory=wm,
    )

    # Task MUST NOT be marked complete: plan has next navigation step to Flipkart!
    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "navigate"
    assert "flipkart.com" in plan.steps[0].action.params.url
    assert plan.blocked is False


# --- 2. Two of three visited: task must remain incomplete ---------------------

def test_two_of_three_websites_visited_remains_incomplete():
    tracker = init_multi_site_tracker(COMPARISON_INSTRUCTION)
    # Amazon completed
    tracker["required_sites"]["amazon"]["status"] = "completed"
    tracker["required_sites"]["amazon"]["data"] = {
        "domain": "amazon.in",
        "url": "https://www.amazon.in/samsung-a27",
        "title": "Samsung Galaxy A27 5G",
        "price": "₹30,700",
        "variant": "128GB, 8GB RAM",
    }
    # Flipkart completed
    tracker["required_sites"]["flipkart"]["status"] = "completed"
    tracker["required_sites"]["flipkart"]["data"] = {
        "domain": "flipkart.com",
        "url": "https://www.flipkart.com/samsung-a27",
        "title": "Samsung Galaxy A27 (128 GB)",
        "price": "₹29,999",
        "variant": "128GB, 8GB RAM",
    }
    # Samsung Store is still pending
    assert tracker["required_sites"]["samsung"]["status"] == "pending"

    wm = {
        "multi_site_research": tracker,
        "collected_research": {
            "amazon.in": tracker["required_sites"]["amazon"]["data"],
            "flipkart.com": tracker["required_sites"]["flipkart"]["data"],
        },
    }

    # Model prematurely claims completion before visiting Samsung Store
    raw_plan = _completion("Compared Amazon and Flipkart. Flipkart is cheaper.")
    plan = _validate_plan(
        raw_plan,
        payload=_payload("https://www.flipkart.com/samsung-a27", title="Samsung Galaxy A27", instruction=COMPARISON_INSTRUCTION),
        history=[
            {"action": {"type": "navigate", "params": {"url": "https://www.amazon.in"}}, "result_ok": True},
            {"action": {"type": "navigate", "params": {"url": "https://www.flipkart.com"}}, "result_ok": True},
        ],
        instruction=COMPARISON_INSTRUCTION,
        working_memory=wm,
    )

    # Task MUST NOT complete: must emit navigation to Samsung Store!
    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "navigate"
    assert "samsung.com" in plan.steps[0].action.params.url


# --- 3. All three visited and results extracted: comparison can complete -----

def test_all_three_visited_and_results_extracted_comparison_completes():
    tracker = init_multi_site_tracker(COMPARISON_INSTRUCTION)
    tracker["required_sites"]["amazon"]["status"] = "completed"
    tracker["required_sites"]["amazon"]["data"] = {
        "domain": "amazon.in",
        "url": "https://www.amazon.in/samsung-a27",
        "title": "Samsung Galaxy A27 5G",
        "price": "₹30,700",
        "variant": "128GB, 8GB RAM",
    }
    tracker["required_sites"]["flipkart"]["status"] = "completed"
    tracker["required_sites"]["flipkart"]["data"] = {
        "domain": "flipkart.com",
        "url": "https://www.flipkart.com/samsung-a27",
        "title": "Samsung Galaxy A27 (128 GB)",
        "price": "₹29,999",
        "variant": "128GB, 8GB RAM",
    }
    tracker["required_sites"]["samsung"]["status"] = "completed"
    tracker["required_sites"]["samsung"]["data"] = {
        "domain": "samsung.com",
        "url": "https://www.samsung.com/in/smartphones/galaxy-a27",
        "title": "Galaxy A27 | Samsung India",
        "price": "₹31,500",
        "variant": "128GB, 8GB RAM",
    }

    assert is_multi_site_task_complete(tracker) is True

    wm = {
        "multi_site_research": tracker,
        "collected_research": {
            "amazon.in": tracker["required_sites"]["amazon"]["data"],
            "flipkart.com": tracker["required_sites"]["flipkart"]["data"],
            "samsung.com": tracker["required_sites"]["samsung"]["data"],
        },
    }

    raw_plan = _completion("Done researching all three websites.")
    plan = _validate_plan(
        raw_plan,
        payload=_payload("https://www.samsung.com/in/smartphones/galaxy-a27", instruction=COMPARISON_INSTRUCTION),
        history=[{"action": {"type": "navigate"}, "result_ok": True}],
        instruction=COMPARISON_INSTRUCTION,
        working_memory=wm,
    )

    # All three resolved: plan can now complete with 0 steps
    assert len(plan.steps) == 0
    assert plan.blocked is False
    # Final answer MUST contain structured comparison table and identify Flipkart as cheapest
    assert "Price & Availability Comparison" in plan.answer
    assert "₹29,999" in plan.answer
    assert "₹30,700" in plan.answer
    assert "₹31,500" in plan.answer
    assert "Flipkart" in plan.answer
    assert "Cheapest Option" in plan.answer


# --- 4. One website unavailable: report limitation and continue --------------

def test_one_website_unavailable_reports_limitation_and_continues():
    tracker = init_multi_site_tracker(COMPARISON_INSTRUCTION)
    # Amazon has verified listing
    tracker["required_sites"]["amazon"]["status"] = "completed"
    tracker["required_sites"]["amazon"]["data"] = {
        "domain": "amazon.in",
        "url": "https://www.amazon.in/samsung-a27",
        "title": "Samsung Galaxy A27 5G",
        "price": "₹30,700",
        "variant": "128GB, 8GB RAM",
    }
    # Flipkart is unavailable (no matching product found)
    tracker["required_sites"]["flipkart"]["status"] = "unavailable"
    tracker["required_sites"]["flipkart"]["reason"] = "No matching product found on Flipkart"

    # Samsung Store is completed
    tracker["required_sites"]["samsung"]["status"] = "completed"
    tracker["required_sites"]["samsung"]["data"] = {
        "domain": "samsung.com",
        "url": "https://www.samsung.com/in/smartphones/galaxy-a27",
        "title": "Galaxy A27 | Samsung India",
        "price": "₹31,500",
        "variant": "128GB, 8GB RAM",
    }

    assert is_multi_site_task_complete(tracker) is True

    wm = {"multi_site_research": tracker}
    raw_plan = _completion("Research finished.")
    plan = _validate_plan(
        raw_plan,
        payload=_payload("https://www.samsung.com/in/", instruction=COMPARISON_INSTRUCTION),
        history=[],
        instruction=COMPARISON_INSTRUCTION,
        working_memory=wm,
    )

    assert len(plan.steps) == 0
    assert plan.blocked is False
    # Verified: Amazon and Samsung are verified, Flipkart is clearly marked Unavailable
    assert "| **Flipkart** | Unavailable |" in plan.answer
    assert "| **Amazon** | Verified | ₹30,700 |" in plan.answer
    assert "| **Samsung Store** | Verified | ₹31,500 |" in plan.answer
    # Cheapest between the verified two is Amazon
    assert "**Cheapest Option**: **Amazon** at ₹30,700" in plan.answer


# --- 5. Provider failure during comparison: resume without repeating ---------

def test_provider_failure_during_comparison_resumes_without_repeating():
    db.init_db()
    chat_id = "test-failover-chat-multi-site"
    task_id = "task-failover-ms-01"

    # 1. State before crash: Amazon completed
    tracker = init_multi_site_tracker(COMPARISON_INSTRUCTION)
    tracker["required_sites"]["amazon"]["status"] = "completed"
    tracker["required_sites"]["amazon"]["data"] = {
        "domain": "amazon.in",
        "url": "https://www.amazon.in/samsung-a27",
        "title": "Samsung Galaxy A27",
        "price": "₹30,700",
    }
    history = [
        {"action": {"type": "navigate", "params": {"url": "https://www.amazon.in"}}, "result_ok": True, "evidence": "amazon loaded"},
        {"action": {"type": "type", "params": {"text": "Samsung Galaxy A27", "submit": True}}, "result_ok": True, "evidence": "typed"},
    ]
    wm = {
        "multi_site_research": tracker,
        "collected_research": {"amazon.in": tracker["required_sites"]["amazon"]["data"]},
    }

    # Save checkpoint representing state before crash
    db.save_task_checkpoint(
        task_id=task_id,
        conversation_id=chat_id,
        session_id="session-ms-01",
        step_index=2,
        last_url="https://www.amazon.in/samsung-a27",
        history=history,
        working_memory=wm,
    )

    # 2. Resumption retrieves checkpoint
    latest_cp = db.get_latest_checkpoint_for_chat(chat_id)
    assert latest_cp is not None
    restored_wm = latest_cp.get("working_memory") or {}
    restored_tracker = restored_wm.get("multi_site_research")
    assert restored_tracker is not None
    # Amazon is already completed!
    assert restored_tracker["required_sites"]["amazon"]["status"] == "completed"
    assert restored_tracker["required_sites"]["flipkart"]["status"] == "pending"

    # 3. Next plan step when resumed: should NOT repeat Amazon, should navigate to Flipkart
    raw_plan = _completion("Trying to finish")
    plan = _validate_plan(
        raw_plan,
        payload=_payload("https://www.amazon.in/samsung-a27", instruction=COMPARISON_INSTRUCTION),
        history=latest_cp.get("history") or [],
        instruction=COMPARISON_INSTRUCTION,
        working_memory=restored_wm,
    )

    assert len(plan.steps) == 1
    assert plan.steps[0].action.type == "navigate"
    # Target must be Flipkart, NOT Amazon!
    assert "flipkart.com" in plan.steps[0].action.params.url


# --- 6. Never claim cheapest option based on only one listing ----------------

def test_never_claim_cheapest_on_only_one_listing():
    tracker = init_multi_site_tracker(COMPARISON_INSTRUCTION)
    # Only Amazon has a listing
    tracker["required_sites"]["amazon"]["status"] = "completed"
    tracker["required_sites"]["amazon"]["data"] = {
        "domain": "amazon.in",
        "url": "https://www.amazon.in/samsung-a27",
        "title": "Samsung Galaxy A27 5G",
        "price": "₹30,700",
    }
    # Both Flipkart and Samsung Store are unavailable
    tracker["required_sites"]["flipkart"]["status"] = "unavailable"
    tracker["required_sites"]["flipkart"]["reason"] = "Out of stock"
    tracker["required_sites"]["samsung"]["status"] = "unavailable"
    tracker["required_sites"]["samsung"]["reason"] = "Product not found"

    wm = {"multi_site_research": tracker}

    # Model tries to claim "Amazon is the cheapest option"
    raw_plan = _completion("Amazon is the cheapest option at ₹30,700.")
    plan = _validate_plan(
        raw_plan,
        payload=_payload("https://www.amazon.in/samsung-a27", instruction=COMPARISON_INSTRUCTION),
        history=[],
        instruction=COMPARISON_INSTRUCTION,
        working_memory=wm,
    )

    assert len(plan.steps) == 0
    assert plan.blocked is False
    # Must NOT claim Amazon is the cheapest option
    assert "Cheapest Option" not in plan.answer
    assert "A comparative cheapest option cannot be determined" in plan.answer or "cannot be claimed" in plan.answer


# --- 7. Prevent unnecessary navigation back to already visited websites ------

def test_prevent_unnecessary_navigation_back_to_completed_site():
    tracker = init_multi_site_tracker(COMPARISON_INSTRUCTION)
    tracker["required_sites"]["amazon"]["status"] = "completed"
    tracker["required_sites"]["amazon"]["data"] = {"domain": "amazon.in", "price": "₹30,700"}
    tracker["required_sites"]["flipkart"]["status"] = "pending"

    assert is_already_resolved_site("https://www.amazon.in/s?k=a27", tracker) is True
    assert is_already_resolved_site("https://www.flipkart.com/search", tracker) is False

    wm = {"multi_site_research": tracker}

    # Model incorrectly tries to navigate back to Amazon
    raw_plan = {
        "goal": "Re-check Amazon",
        "steps": [{
            "why": "navigating to Amazon again",
            "action": {"type": "navigate", "risk": "write", "params": {"url": "https://www.amazon.in"}, "timeoutMs": 10000},
        }],
    }
    plan = _validate_plan(
        raw_plan,
        payload=_payload("https://www.google.com", instruction=COMPARISON_INSTRUCTION),
        history=[],
        instruction=COMPARISON_INSTRUCTION,
        working_memory=wm,
    )

    # Redirection backstop: action redirected to Flipkart instead of revisiting Amazon!
    assert len(plan.steps) == 1
    assert "flipkart.com" in plan.steps[0].action.params.url
    assert "Prevented redundant visit" in plan.steps[0].why


# --- 8. Distinguish verified, unavailable, and unvisited websites -------------

def test_distinguish_verified_unavailable_and_unvisited_sites():
    tracker = init_multi_site_tracker(COMPARISON_INSTRUCTION)
    tracker["required_sites"]["amazon"]["status"] = "completed"
    tracker["required_sites"]["amazon"]["data"] = {
        "domain": "amazon.in",
        "url": "https://www.amazon.in/samsung-a27",
        "price": "₹30,700",
        "variant": "128GB",
    }
    tracker["required_sites"]["flipkart"]["status"] = "unavailable"
    tracker["required_sites"]["flipkart"]["reason"] = "0 results found"
    tracker["required_sites"]["samsung"]["status"] = "pending"  # unvisited

    table = format_comparison_table(tracker, COMPARISON_INSTRUCTION)
    assert "| **Amazon** | Verified | ₹30,700 |" in table
    assert "| **Flipkart** | Unavailable | N/A | 0 results found |" in table
    assert "| **Samsung Store** | Unvisited | N/A | Not visited |" in table

