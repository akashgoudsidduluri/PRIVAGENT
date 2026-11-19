import asyncio
import json
import time

from fastapi.testclient import TestClient
import pytest

from app import db
from app.change_detector import evaluate_observation, broadcast_event
from app.control_center import create_mission, MonitorInput
from app.hub import hub
from app.main import app
from app.monitoring_scheduler import claim_due, next_due_at, execute_monitor


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "monitor.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _mission(condition="text_change", params=None):
    return create_mission(MonitorInput(title="Price watch", target_url="https://example.org/product",
                                       interval_seconds=60, condition_type=condition, condition_params=params or {}))["id"]


def test_persistence_claim_and_nonduplicate_text_events(database):
    mission_id = _mission()
    client = TestClient(app)
    assert client.patch(f"/api/control/missions/{mission_id}", json={"status": "active"}).status_code == 200
    due = claim_due(now=time.time() + 1, db_path=database)
    assert len(due) == 1 and due[0]["id"] == mission_id
    assert claim_due(now=time.time() + 1, db_path=database) == []
    assert evaluate_observation(mission_id, {"text": "₹100"}, observed_at=100, db_path=database) is None
    event = evaluate_observation(mission_id, {"text": "₹90"}, observed_at=160, db_path=database)
    assert event is not None and event["diff_data"]["old_price"] == 100
    assert evaluate_observation(mission_id, {"text": "₹90"}, observed_at=220, db_path=database) is None
    with db.get_connection(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitoring_events WHERE mission_id=?", (mission_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT next_run_at FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone()[0] == 280
    # A new process/connection sees the event and persists its acknowledgement.
    events = client.get(f"/api/control/missions/{mission_id}/events").json()
    assert len(events) == 1
    assert client.post(f"/api/control/missions/{mission_id}/events/{events[0]['id']}/ack").json() == {"ok": True}


def test_price_regex_element_and_sensitive_data(database):
    price = _mission("price_drop", {"min_percent": 10})
    assert evaluate_observation(price, {"text": "₹100"}, observed_at=100) is None
    assert evaluate_observation(price, {"text": "₹95"}, observed_at=160) is None
    assert evaluate_observation(price, {"text": "₹80"}, observed_at=220) is not None
    assert evaluate_observation(price, {"text": "₹80"}, observed_at=280) is None
    regex = _mission("regex", {"pattern": "in stock"})
    evaluate_observation(regex, {"text": "Sold out"}, observed_at=100)
    assert evaluate_observation(regex, {"text": "In stock"}, observed_at=160)
    element = _mission("element", {"name": "Buy now"})
    evaluate_observation(element, {"text": "Product", "elements": ["Details"]}, observed_at=100)
    assert evaluate_observation(element, {"text": "Product", "elements": ["Buy now"]}, observed_at=160)
    sensitive = _mission()
    evaluate_observation(sensitive, {"text": "Contact me at someone@example.org"}, observed_at=100)
    with db.get_connection(database) as conn:
        state = conn.execute("SELECT last_state_json FROM monitoring_missions WHERE id=?", (sensitive,)).fetchone()[0]
    assert "someone@example.org" not in state


def test_cron_webhook_and_alert_delivery(database, monkeypatch):
    now = 1_800_000_000
    due = next_due_at("cron", "*/5 * * * *", 60, now)
    assert due > now and (due - now) <= 300
    assert next_due_at("webhook", None, 60, now) is None
    client = TestClient(app)
    created = client.post("/api/control/missions", json={"title": "Webhook", "target_url": "https://example.org/",
        "trigger_type": "webhook", "interval_seconds": 60})
    assert created.status_code == 201
    ident = created.json()["id"]
    assert client.patch(f"/api/control/missions/{ident}", json={"status": "active"}).status_code == 200
    assert client.post(f"/api/control/missions/{ident}/trigger").status_code == 503
    monkeypatch.setenv("PRIVAGENT_MONITOR_WEBHOOK_SECRET", "test-secret")
    assert client.post(f"/api/control/missions/{ident}/trigger", headers={"X-Privagent-Token": "wrong"}).status_code == 403
    assert client.post(f"/api/control/missions/{ident}/trigger", headers={"X-Privagent-Token": "test-secret"}).json() == {"queued": True}
    assert [m["id"] for m in claim_due(now=time.time() + 1)] == [ident]
    assert claim_due(now=time.time() + 1) == []
    class WS:
        async def send_json(self, value):
            self.value = value
    ws = WS()
    hub.client_sockets.append(ws)
    try:
        event = evaluate_observation(ident, {"text": "A"}, observed_at=100)
        assert event is None
        event = evaluate_observation(ident, {"text": "B"}, observed_at=160)
        asyncio.run(broadcast_event(event))
        assert ws.value["type"] == "MISSION_EVENT_TRIGGERED"
        with db.get_connection(database) as conn:
            assert conn.execute("SELECT notification_sent FROM monitoring_events WHERE id=?", (event["id"],)).fetchone()[0] == 1
    finally:
        hub.client_sockets.remove(ws)


def test_scheduler_dispatches_task_record_then_detects_change(database, monkeypatch):
    mission_id = _mission()
    evaluate_observation(mission_id, {"text": "Before"}, observed_at=100)
    async def fake_run(task_id):
        record = hub.tasks[task_id]
        record.state = "completed"
        record.last_observation = {"url": "https://example.org/product", "pageText": "After", "interactive": []}
    monkeypatch.setattr("app.agentLoop.run_task", fake_run)
    with db.get_connection(database) as conn:
        mission = dict(conn.execute("SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone())
    asyncio.run(execute_monitor(mission))
    with db.get_connection(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitoring_events WHERE mission_id=?", (mission_id,)).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Fix 6: absolute below-price trigger. A threshold is a state, not a
# change — being under it alerts on the first run (the product may
# already be cheap), then deduplicates while the state is unchanged.
# ---------------------------------------------------------------------------

def test_below_price_trigger_boundaries():
    from app.change_detector import _triggered

    assert _triggered("below_price", {"threshold": 5000}, {}, {"price": 4999})[0] is True
    assert _triggered("below_price", {"threshold": 5000}, {}, {"price": 5000})[0] is False
    assert _triggered("below_price", {"threshold": 5000}, {}, {"price": 5001})[0] is False
    assert _triggered("below_price", {"threshold": 5000}, {}, {"price": None})[0] is False
    with pytest.raises(ValueError):
        _triggered("below_price", {}, {}, {"price": 100})
    with pytest.raises(ValueError):
        _triggered("below_price", {"threshold": 0}, {}, {"price": 100})
    with pytest.raises(ValueError):
        _triggered("below_price", {"threshold": -5}, {}, {"price": 100})


# The user's exact test-case phrasing ("check the halio strap product
# and tell me if it reaches <5000 notify me everyday") must create a
# real below-price watch. Before the fix, parse_below_price only knew
# "below/under/less than/at most ₹X", so "reaches <5000" returned
# None and the ask fell through to a generic price-drop watch; and
# parse_daily_time needed a clock time, so a bare "every day"
# returned None and the cadence fell back to a 5-minute interval.
def test_parse_below_price_user_phrasings():
    from app.continuous_monitoring_companion import parse_below_price

    assert parse_below_price("check the halio strap product and tell me if it reaches <5000 notify me everyday") == 5000.0
    assert parse_below_price("tell me if it reaches <5000") == 5000.0
    assert parse_below_price("alert me if it reaches below ₹5000") == 5000.0
    assert parse_below_price("notify me when it drops to ₹4,999") == 4999.0
    assert parse_below_price("alert me when the price falls below 5000") == 5000.0
    assert parse_below_price("watch it and alert me if it goes under ₹5000") == 5000.0
    assert parse_below_price("tell me when it is <₹5000") == 5000.0
    # Existing form still works (regression).
    assert parse_below_price("alert me when the price falls below ₹5000") == 5000.0
    assert parse_below_price("alert me if it goes below 5000") == 5000.0
    # No threshold named: None, never a number invented from a price.
    assert parse_below_price("check the price of the Amazfit Helio Strap") is None
    assert parse_below_price("tell me the price every day") is None


def test_parse_daily_time_bare_every_day_defaults_9am():
    from app.continuous_monitoring_companion import parse_daily_time

    # Bare daily cadence (no clock time) defaults to the 9:00 AM IST
    # morning report — the same default the daily digest uses.
    assert parse_daily_time("notify me everyday") == ("30 3 * * *", "9:00 AM IST")
    assert parse_daily_time("check the halio strap product and tell me if it reaches <5000 notify me everyday") == ("30 3 * * *", "9:00 AM IST")
    assert parse_daily_time("check it daily") == ("30 3 * * *", "9:00 AM IST")
    # Timed forms still work (regression).
    assert parse_daily_time("tell me the price every day at 9am") == ("30 3 * * *", "9:00 AM IST")
    assert parse_daily_time("tell me the price every day at 6pm") == ("30 12 * * *", "6:00 PM IST")
    assert parse_daily_time("at 9am every day tell me the price") == ("30 3 * * *", "9:00 AM IST")
    # No daily cadence at all: None.
    assert parse_daily_time("check the price now") is None
    assert parse_daily_time("alert me when it drops below ₹5000") is None


def test_extract_monitored_product_prefers_instruction_url():
    from app.continuous_monitoring_companion import _extract_monitored_product

    amazon_url = "https://www.amazon.in/dp/B0F8HJCB47"
    # The URL pasted in the instruction itself wins.
    title, url = _extract_monitored_product(
        f"Check the Amazfit Helio Strap on {amazon_url} and tell me if it reaches <5000"
    )
    assert title == "Amazfit Helio Strap"
    assert url == amazon_url
    # The instruction URL wins even when the history has a different
    # store URL (a Flipkart search page from an earlier suggestion).
    title, url = _extract_monitored_product(
        f"Check the Amazfit Helio Strap on {amazon_url} and tell me if it reaches <5000",
        history=[{"role": "assistant", "content": "I found the Samsung Galaxy Fit3 on https://www.flipkart.com/search?q=samsung+galaxy+fit3"}],
    )
    assert title == "Amazfit Helio Strap"
    assert url == amazon_url
    # No URL anywhere: falls back to a search page (existing behaviour).
    title, url = _extract_monitored_product("Check the Amazfit Helio Strap and tell me if it reaches <5000")
    assert title == "Amazfit Helio Strap"
    assert url.startswith("https://www.flipkart.com/search?q=")


def test_handler_composes_daily_below_price(database):
    from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction

    amazon_url = "https://www.amazon.in/dp/B0F8HJCB47"
    instruction = (
        f"Check the Amazfit Helio Strap on {amazon_url} "
        "and tell me if it reaches <5000 notify me everyday"
    )
    reply = asyncio.run(handle_continuous_monitoring_instruction(instruction, chat_id="t1", db_path=database))
    assert "below" in reply.lower() and "5000" in reply
    with db.get_connection(database) as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_missions WHERE conversation_id='t1'"
        ).fetchone()
    assert row is not None
    assert row["title"] == "Price Watch: Amazfit Helio Strap"
    assert row["target_url"] == amazon_url
    # The composition: a below_price condition on a DAILY cron, not a
    # daily_price report and not a 5-minute interval watch.
    assert row["condition_type"] == "below_price"
    assert json.loads(row["condition_params"]) == {"target": "Amazfit Helio Strap", "threshold": 5000.0}
    assert row["trigger_type"] == "cron"
    assert row["schedule_cron"] == "30 3 * * *"
    assert row["interval_seconds"] == 86400
    assert row["status"] == "active"
    # The scheduler will claim a cron below_price mission when due.
    assert next_due_at("cron", "30 3 * * *", 86400, time.time()) is not None


def test_handler_below_only_stays_interval(database):
    from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction

    reply = asyncio.run(handle_continuous_monitoring_instruction(
        "Alert me when the Amazfit Helio Strap on https://www.amazon.in/dp/B0F8HJCB47 falls below ₹5000",
        chat_id="t2", db_path=database,
    ))
    assert "5000" in reply
    with db.get_connection(database) as conn:
        row = conn.execute("SELECT * FROM monitoring_missions WHERE conversation_id='t2'").fetchone()
    assert row["condition_type"] == "below_price"
    assert json.loads(row["condition_params"])["threshold"] == 5000.0
    assert row["trigger_type"] == "interval"
    assert row["schedule_cron"] is None
    assert row["interval_seconds"] == 300


def test_handler_daily_only_stays_daily_price(database):
    from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction

    reply = asyncio.run(handle_continuous_monitoring_instruction(
        "Tell me the Amazfit Helio Strap price every day at 9am",
        chat_id="t3", db_path=database,
    ))
    assert "9:00 AM" in reply
    with db.get_connection(database) as conn:
        row = conn.execute("SELECT * FROM monitoring_missions WHERE conversation_id='t3'").fetchone()
    assert row["condition_type"] == "daily_price"
    assert row["trigger_type"] == "cron"
    assert row["schedule_cron"] == "30 3 * * *"



def test_directive_routes_user_exact_phrasing():
    """The user's exact test-case phrasing must ROUTE to the monitoring
    handler. Before the fix, MONITORING_DIRECTIVE_PATTERN only knew
    'monitor/track/watch/alert + below ₹X' and 'notify me + price', so
    'check ... reaches <5000 notify me everyday' (verb 'check',
    threshold '<5000', no word 'price') fell through to a browser
    task that only looks once — and in the sandbox that fails with
    'Chrome extension is not connected', so no watch was ever made."""
    from app.continuous_monitoring_companion import is_continuous_monitoring_directive

    assert is_continuous_monitoring_directive(
        "Check the Amazfit Helio Strap on https://www.amazon.in/dp/B0F8HJCB47 "
        "and tell me if it reaches <5000 notify me everyday"
    ) is True
    assert is_continuous_monitoring_directive(
        "Check the Amazfit Helio Strap and tell me if it reaches <5000"
    ) is True
    assert is_continuous_monitoring_directive(
        "notify me when the Amazfit Helio Strap drops to ₹5000"
    ) is True
    assert is_continuous_monitoring_directive(
        "Watch the Amazfit Helio Strap and alert me when the price drops below ₹5000, notify me every day"
    ) is True
    # Controls: a plain statement or a one-off look must NOT route.
    assert is_continuous_monitoring_directive("the Amazfit Helio Strap price is below 5000") is False
    assert is_continuous_monitoring_directive("check the Amazfit Helio Strap") is False
    assert is_continuous_monitoring_directive("what is the price of the Amazfit Helio Strap") is False


def test_extract_monitored_product_other_brands():
    """The watch flow is generic, not Helio-only: any brand+model
    works. 'Logitech' was not in the brand list, so 'Logitech MX
    Master 3S' fell through to the weak-signal regex and got titled
    by the store name instead of the product."""
    from app.continuous_monitoring_companion import _extract_monitored_product

    title, url = _extract_monitored_product(
        "Watch the Logitech MX Master 3S on "
        "https://www.flipkart.com/logitech-mx-master-3s-wireless-touch-mouse/p/itm271ade01ff274?pid=ACCGFPFCAFGREFHZ "
        "and alert me if it drops under ₹8000"
    )
    assert title == "Logitech Mx Master 3S"
    assert url.startswith("https://www.flipkart.com/logitech-mx-master-3s-wireless-touch-mouse/p/itm271ade01ff274")
    # Other brands work the same way.
    title, _ = _extract_monitored_product("Watch the HP Victus 15 and alert me if it drops below ₹60000")
    assert title == "Hp Victus 15"


def test_extract_monitored_product_monitor_this_phrase():
    """A product named after \"monitor this\" — with no brand in the
    brand list — must still be extracted. Live failure (2026-11-19):
    the user asked to monitor a \"Grandbiker Rear Wheel Paddock
    Stand...\" and the agent created a watch for \"Google Calendar\"
    instead, because the history scan matched the brand \"google\" in
    the agent's own greeting (\"Connected apps — Gmail, Google
    Calendar, Drive...\") while the instruction-side extraction had no
    verb-phrase path for \"monitor this, <product>\"."""
    from app.continuous_monitoring_companion import _extract_monitored_product

    title, url = _extract_monitored_product(
        "monitor this, Grandbiker Rear Wheel Paddock Stand with Swing Arm Rest "
        "Bike Storage Stand (Floor Mount). if the price is below 1000 then alert me."
    )
    assert title == "Grandbiker Rear Wheel Paddock Stand With Swing Arm Rest Bike Storage Stand", title  # titles are capped at 80 chars at a word boundary (see _truncate_title)
    assert url.startswith("https://www.flipkart.com/search?q=Grandbiker"), url


def test_extract_monitored_product_ignores_greeting_service_names():
    """The greeting alone must yield NO product: \"Google Calendar\"
    in \"Connected apps — Gmail, Google Calendar, Drive, Slack,
    GitHub...\" is a connected service, not a product to watch. The
    same denylist also stops \"check its price\" from becoming the
    nonsense product \"s price\" (the verb-phrase regex used to match
    \"it\" inside \"its\")."""
    from app.continuous_monitoring_companion import _extract_monitored_product, _product_phrase_from_text

    greeting = (
        "Hey there! Great to see you. I'm PrivAgent — your privacy-preserving browser co-pilot. "
        "Connected apps — Gmail, Google Calendar, Drive, Slack, GitHub, and your local files."
    )
    assert _extract_monitored_product(
        "hi", history=[{"role": "assistant", "content": greeting}]
    ) == ("", "")
    assert _product_phrase_from_text(
        "or do one thing, check its price and just alert me every 5 minutes"
    ) == ""


def test_follow_up_cadence_change_resolves_product_from_history():
    """\"or do one thing, check its price and just alert me every 5
    minutes\" is a monitoring directive (the interval branch was
    missing, so it fell through to a research task and failed with
    \"Research incomplete\"), and it resolves the product from the
    earlier turn — the Grandbiker stand — with the earlier threshold
    (below ₹1000)."""
    from app.continuous_monitoring_companion import (
        _extract_monitored_product,
        _threshold_from_history,
        is_continuous_monitoring_directive,
    )

    monitor_ask = (
        "monitor this, Grandbiker Rear Wheel Paddock Stand with Swing Arm Rest "
        "Bike Storage Stand (Floor Mount). if the price is below 1000 then alert me."
    )
    greeting = (
        "Hey there! Great to see you. I'm PrivAgent. "
        "Connected apps — Gmail, Google Calendar, Drive, Slack, GitHub, and your local files."
    )
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": greeting},
        {"role": "user", "content": "so can you really perform the monitoring"},
        {"role": "user", "content": monitor_ask},
    ]
    follow_up = "or do one thing, check its price and just alert me every 5 minutes"
    assert is_continuous_monitoring_directive(follow_up) is True
    title, _ = _extract_monitored_product(follow_up, history=history)
    assert title == "Grandbiker Rear Wheel Paddock Stand With Swing Arm Rest Bike Storage Stand", title  # titles are capped at 80 chars at a word boundary (see _truncate_title)
    assert _threshold_from_history(history) == 1000.0


def test_handler_does_not_duplicate_existing_price_watch(database):
    """A cadence-only follow-up must not create a SECOND watch for the
    same product: the first watch already checks every 5 minutes, so
    a duplicate would double-alert on the same drop. The handler
    answers \"already running\" and leaves exactly one active
    mission."""
    from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction

    monitor_ask = (
        "monitor this, Grandbiker Rear Wheel Paddock Stand with Swing Arm Rest "
        "Bike Storage Stand (Floor Mount). if the price is below 1000 then alert me."
    )
    reply1 = asyncio.run(handle_continuous_monitoring_instruction(monitor_ask, chat_id="dup1", db_path=database))
    assert "1000" in reply1
    follow_up = "or do one thing, check its price and just alert me every 5 minutes"
    history = [{"role": "user", "content": monitor_ask}]
    reply2 = asyncio.run(handle_continuous_monitoring_instruction(
        follow_up, chat_id="dup1", history=history, db_path=database,
    ))
    assert "already running" in reply2.lower(), reply2
    with db.get_connection(database) as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_missions WHERE conversation_id='dup1' AND status='active'"
        ).fetchall()
    assert len(rows) == 1, rows
    assert rows[0]["title"] == "Price Watch: Grandbiker Rear Wheel Paddock Stand With Swing Arm Rest Bike Storage Stand"
    assert rows[0]["interval_seconds"] == 300


def test_handler_bare_cadence_follow_up_updates_existing_watch(database):
    """A bare cadence follow-up ("check every 2 hours") must update
    the existing watch, not create a phantom one. Live shape of the
    bug, found by tracing the exact follow-up through the handler:
    the phrase after "check" was "every 2 hours", which the phrase
    extractor kept as a product name — so the handler created a
    SECOND watch titled "Every 2 Hours" (price_drop, no threshold)
    while the real Grandbiker watch stayed on 5 minutes and the
    user's requested change silently never happened."""
    from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction

    monitor_ask = (
        "monitor this, Grandbiker Rear Wheel Paddock Stand with Swing Arm Rest "
        "Bike Storage Stand (Floor Mount). if the price is below 1000 then alert me."
    )
    reply1 = asyncio.run(handle_continuous_monitoring_instruction(monitor_ask, chat_id="bare1", db_path=database))
    assert "1000" in reply1
    history = [{"role": "user", "content": monitor_ask}, {"role": "assistant", "content": reply1}]
    reply2 = asyncio.run(handle_continuous_monitoring_instruction(
        "check every 2 hours", chat_id="bare1", history=history, db_path=database,
    ))
    assert "updated" in reply2.lower(), reply2
    assert "Grandbiker" in reply2, reply2
    assert "every 2 hours" in reply2.lower(), reply2
    assert "Every 2 Hours" not in reply2, reply2  # no phantom product title
    with db.get_connection(database) as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_missions WHERE conversation_id='bare1' AND status='active'"
        ).fetchall()
    assert len(rows) == 1, rows
    assert "Every 2 Hours" not in rows[0]["title"], rows[0]["title"]
    assert rows[0]["interval_seconds"] == 7200
    assert rows[0]["condition_type"] == "below_price"
    assert json.loads(rows[0]["condition_params"])["threshold"] == 1000.0


def test_handler_cadence_change_updates_existing_watch(database):
    """A follow-up that CHANGES the cadence must update the existing
    watch, not claim it already runs that way and do nothing. Live
    shape of the bug: a daily cron watch exists; \"check its price
    every 5 minutes\" answered \"is being checked every 5 minutes\"
    and created nothing — the reply was false and the requested change
    was silently dropped. The update happens in place (one active
    mission) and the reply says what changed."""
    from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction

    product = (
        "Grandbiker Rear Wheel Paddock Stand with Swing Arm Rest "
        "Bike Storage Stand (Floor Mount)"
    )
    reply1 = asyncio.run(handle_continuous_monitoring_instruction(
        f"Watch the {product} and tell me the price every day at 9am",
        chat_id="m1upd", db_path=database,
    ))
    assert "9:00 AM" in reply1
    with db.get_connection(database) as conn:
        before = conn.execute(
            "SELECT interval_seconds, condition_type FROM monitoring_missions "
            "WHERE conversation_id='m1upd' AND status='active'"
        ).fetchall()
    assert len(before) == 1 and before[0]["interval_seconds"] == 86400, before
    history = [{"role": "user", "content": f"monitor this, {product}. if the price is below 1000 then alert me."}]
    reply2 = asyncio.run(handle_continuous_monitoring_instruction(
        "or do one thing, check its price and just alert me every 5 minutes",
        chat_id="m1upd", history=history, db_path=database,
    ))
    assert "updated" in reply2.lower(), reply2
    assert "daily" in reply2.lower() and "every 5 minutes" in reply2.lower(), reply2
    with db.get_connection(database) as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_missions WHERE conversation_id='m1upd' AND status='active'"
        ).fetchall()
    assert len(rows) == 1, rows
    assert rows[0]["interval_seconds"] == 300
    assert rows[0]["condition_type"] == "below_price"
    assert json.loads(rows[0]["condition_params"])["threshold"] == 1000.0


def test_parse_interval_seconds_words_and_hours():
    """\"every two hours\" is 7200 seconds, \"every five minutes\" is
    300 — the handler used to hardcode 300s for every interval watch,
    so an hourly cadence silently became a 5-minute one."""
    from app.continuous_monitoring_companion import _parse_interval_seconds, _describe_interval

    assert _parse_interval_seconds("check every 5 minutes") == 300
    assert _parse_interval_seconds("alert me every five minutes") == 300
    assert _parse_interval_seconds("check every two hours") == 7200
    assert _parse_interval_seconds("check every 1 hour") == 3600
    assert _parse_interval_seconds("no cadence here") == 300
    # A bare "every hour" / "every minute" (no number) means one of
    # the unit — and it routes as a monitoring directive.
    assert _parse_interval_seconds("check its price every hour") == 3600
    assert _parse_interval_seconds("alert me every minute") == 60
    from app.continuous_monitoring_companion import is_continuous_monitoring_directive

    assert is_continuous_monitoring_directive("check its price every hour") is True
    assert _describe_interval(7200) == "every 2 hours"
    assert _describe_interval(300) == "every 5 minutes"


def test_product_phrase_tails_and_pronouns():
    """Phrase tails and pronoun-led phrases must not become products:
    a comma before \"alert me\" (\"..., alert me\") used to mangle the
    title to \"..., Alert Me\"; \"the price of the X\" left a leading
    \"the\"; \"check out ...\" and \"watch my email\" became bogus
    products (\"out the new deals\", \"my email\")."""
    from app.continuous_monitoring_companion import _product_phrase_from_text

    assert _product_phrase_from_text("watch the Grandbiker Rear Stand, alert me") == "Grandbiker Rear Stand"
    assert _product_phrase_from_text("check the price of the Grandbiker Rear Stand") == "Grandbiker Rear Stand"
    assert _product_phrase_from_text("check out the new deals on laptops") == ""
    assert _product_phrase_from_text("watch my email for offers") == ""
    assert _product_phrase_from_text("check your inbox for price drops") == ""
    # A plural service name is still a service ("Google Calendars").
    from app.continuous_monitoring_companion import _is_service_name

    assert _is_service_name("Google Calendars") is True
    assert _is_service_name("Google Calendar app") is True
    assert _is_service_name("Samsung Galaxy Fit3") is False
    # A bare cadence is not a product: "check every 2 hours" / "check
    # every day" used to become a phantom watch titled "Every 2 Hours"
    # / "Every Day", and the real watch never changed cadence.
    assert _product_phrase_from_text("check every 2 hours") == ""
    assert _product_phrase_from_text("check every five minutes") == ""
    assert _product_phrase_from_text("check every day") == ""
    assert _product_phrase_from_text("check every morning") == ""
    assert _product_phrase_from_text("watch the bike stand every day") == "bike stand"
    assert _product_phrase_from_text("monitor the bike stand every 2 hours") == "bike stand"


def test_truncate_title_cuts_at_word_boundary():
    """A long title is capped at 80 chars at a word boundary, never
    mid-word: the Grandbiker title used to end "... Stand (Floo" in
    the watch card and in every reply naming the product."""
    from app.continuous_monitoring_companion import _truncate_title

    long_title = (
        "Grandbiker Rear Wheel Paddock Stand with Swing Arm Rest Bike "
        "Storage Stand (Floor Mount)"
    )
    assert _truncate_title(long_title) == (
        "Grandbiker Rear Wheel Paddock Stand with Swing Arm Rest Bike Storage Stand"
    )
    assert len(_truncate_title(long_title)) <= 80
    assert _truncate_title("Short title") == "Short title"
    assert _truncate_title("x" * 100) == "x" * 80  # no space: hard cut
