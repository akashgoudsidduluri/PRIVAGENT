"""Task 2 live monitoring: HTTP check path, noise filtering, scheduler wiring.

Missions are inserted directly into the SQLite tables instead of via
``create_mission``: that endpoint's public-URL gate does a local DNS
lookup, and this VM's DNS answers every public name with a CGNAT
address, so ``is_safe_public_url`` rejects even example.org here
(the 4 pre-existing test_continuous_monitoring failures). The HTTP
check path under test validates 127.0.0.1 fixture URLs itself.
"""
import asyncio
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
import pytest

from app import db
from app.change_detector import evaluate_observation
from app.hub import hub
from app.main import app
from app.monitor_check import (
    _validate_monitor_url,
    check_monitor_http,
    extract_visible,
    fetch_observation,
)
from app.monitoring_scheduler import claim_due, execute_monitor


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "live-monitor.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _insert_mission(db_path, target_url, condition="text_change", params=None, status="active",
                    trigger="interval", cron=None, interval=60):
    mission_id = str(uuid.uuid4())
    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
               (id, title, target_url, trigger_type, schedule_cron, interval_seconds,
                condition_type, condition_params, status, created_at, next_run_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (mission_id, "Live watch", target_url, trigger, cron, interval,
             condition, json.dumps(params or {}), status, time.time(), time.time() - 1))
    return mission_id


def _events(db_path, mission_id):
    with db.get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM monitoring_events WHERE mission_id=? ORDER BY detected_at",
            (mission_id,)).fetchall()


class _FixtureHandler(BaseHTTPRequestHandler):
    """Serves mutable generic fixture pages; records request methods."""

    pages: dict[str, str] = {}
    methods: list[str] = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence stderr noise
        pass

    def _serve(self):
        _FixtureHandler.methods.append(self.command)
        body = _FixtureHandler.pages.get(self.path)
        if body is None:
            payload = b"not found"
            self.send_response(404)
        else:
            payload = body.encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._serve()


_CANONICAL_PAGES = {
    "/notes": (
        "<html><head><title>Field Notes</title></head><body>"
        "<nav>Home | Archive | Search</nav>"
        "<main><h1>Field Notes</h1>"
        "<p>On-device perception keeps page text local.</p>"
        "<p>Updated 5 minutes ago</p></main>"
        "<footer>Copyright 2026</footer>"
        "<script>var x = 1;</script></body></html>"
    ),
    "/gadget": (
        "<html><head><title>Gadget Store</title></head><body>"
        "<main><h1>Widget Pro</h1>"
        "<p>The Widget Pro is in stock.</p>"
        "<p>Price: \u20b91,299</p>"
        "<p>1,234 views</p>"
        "<button>Buy now</button></main></body></html>"
    ),
}


@pytest.fixture
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    _FixtureHandler.pages = dict(_CANONICAL_PAGES)
    _FixtureHandler.methods = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_http_check_baselines_and_no_change_no_alert(database, fixture_server):
    mission = _insert_mission(database, fixture_server + "/notes")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    assert _events(database, mission) == []
    # The check is read-only: only GETs ever hit the fixture.
    assert set(_FixtureHandler.methods) == {"GET"}


def test_http_extraction_strips_boilerplate_and_keeps_names(database, fixture_server):
    observation = fetch_observation(fixture_server + "/notes")
    assert "Field Notes" in observation["text"]
    assert "On-device perception keeps page text local." in observation["text"]
    assert "Home | Archive" not in observation["text"]
    assert "Copyright 2026" not in observation["text"]
    assert "var x = 1" not in observation["text"]
    assert "Field Notes" in observation["elements"]
    # stdlib-only extraction, no new dependency.
    assert extract_visible("<p>Hello <b>world</b></p>")["text"] == "Hello world"


class _FakeHeaders:
    def __init__(self, ctype="text/html"):
        self._ctype = ctype

    def get_content_type(self):
        return self._ctype

    def get_content_charset(self):
        return "utf-8"


class _FakeResponse:
    def __init__(self, body, ctype="text/html"):
        self._body = body.encode("utf-8")
        self.headers = _FakeHeaders(ctype)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        if not self._body:
            return b""
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk


class _FlakyOpener:
    """Fails the first `failures` opens with a DNS URLError, then serves `body`."""

    def __init__(self, failures, body, ctype="text/html"):
        self.failures = failures
        self.calls = 0
        self.body = body
        self.ctype = ctype

    def open(self, request, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            import urllib.error
            raise urllib.error.URLError("Temporary failure in name resolution")
        return _FakeResponse(self.body, self.ctype)


def test_fetch_retries_transient_dns_blip_then_succeeds(monkeypatch):
    """A transient DNS blip must not fail a watch: the old fetch
    raised on the first URLError, so one blip marked the mission
    failed in the scheduler. Now it retries twice (2s, 5s) first."""
    opener = _FlakyOpener(2, "<html><body><p>Price: ₹1,299</p></body></html>")
    monkeypatch.setattr("app.monitor_check.urllib.request.build_opener", lambda *a, **k: opener)
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(round(float(s), 1)))

    observation = fetch_observation("http://127.0.0.1/gadget")
    assert "1,299" in observation["text"]
    assert opener.calls == 3
    assert sleeps == [2.0, 5.0]


def test_fetch_gives_up_after_three_transient_failures(monkeypatch):
    """A persistently unreachable page still fails honestly — but
    only after all three attempts, not on the first blip."""
    opener = _FlakyOpener(99, "<html><body><p>Price: ₹1,299</p></body></html>")
    monkeypatch.setattr("app.monitor_check.urllib.request.build_opener", lambda *a, **k: opener)
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(round(float(s), 1)))

    with pytest.raises(RuntimeError, match="name resolution"):
        fetch_observation("http://127.0.0.1/gadget")
    assert opener.calls == 3
    assert sleeps == [2.0, 5.0]


def test_fetch_does_not_retry_refusals(monkeypatch):
    """Refusals are definitive, not transient: a non-HTML body raises
    at once with a single attempt and no backoff sleep."""
    opener = _FlakyOpener(0, '{"price": 1299}', ctype="application/json")
    monkeypatch.setattr("app.monitor_check.urllib.request.build_opener", lambda *a, **k: opener)
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(round(float(s), 1)))

    with pytest.raises(ValueError, match="non-HTML"):
        fetch_observation("http://127.0.0.1/gadget")
    assert opener.calls == 1
    assert sleeps == []


def test_noise_timestamp_and_counter_do_not_alert(database, fixture_server):
    mission = _insert_mission(database, fixture_server + "/notes")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    # Only volatile noise changes: a newer timestamp and a higher counter.
    _FixtureHandler.pages["/notes"] = _FixtureHandler.pages["/notes"].replace(
        "Updated 5 minutes ago", "Updated 17 November 2026, 2 minutes ago")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    assert _events(database, mission) == []
    # Same for the store page: only the view counter moves.
    store = _insert_mission(database, fixture_server + "/gadget")
    assert check_monitor_http(store, fixture_server + "/gadget", db_path=database) is None
    _FixtureHandler.pages["/gadget"] = _FixtureHandler.pages["/gadget"].replace("1,234 views", "9,876 views")
    assert check_monitor_http(store, fixture_server + "/gadget", db_path=database) is None
    assert _events(database, store) == []


def test_real_content_change_alerts_with_before_after(database, fixture_server):
    mission = _insert_mission(database, fixture_server + "/notes")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    _FixtureHandler.pages["/notes"] = _FixtureHandler.pages["/notes"].replace(
        "On-device perception keeps page text local.",
        "On-device perception now ships with a local planner.")
    event = check_monitor_http(mission, fixture_server + "/notes", db_path=database)
    assert event is not None
    assert "keeps page text local" in event["diff_data"]["before"]
    assert "local planner" in event["diff_data"]["after"]
    assert len(_events(database, mission)) == 1


def test_price_drop_alerts_and_small_drop_does_not(database, fixture_server):
    mission = _insert_mission(database, fixture_server + "/gadget",
                              condition="price_drop", params={"min_percent": 10})
    assert check_monitor_http(mission, fixture_server + "/gadget", db_path=database) is None
    _FixtureHandler.pages["/gadget"] = _FixtureHandler.pages["/gadget"].replace("\u20b91,299", "\u20b91,199")
    assert check_monitor_http(mission, fixture_server + "/gadget", db_path=database) is None
    _FixtureHandler.pages["/gadget"] = _FixtureHandler.pages["/gadget"].replace("\u20b91,199", "\u20b9" + "999")
    event = check_monitor_http(mission, fixture_server + "/gadget", db_path=database)
    assert event is not None
    assert event["diff_data"]["old_price"] == 1199
    assert event["diff_data"]["new_price"] == 999


def test_dedupe_same_change_does_not_alert_twice(database, fixture_server):
    mission = _insert_mission(database, fixture_server + "/notes")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    _FixtureHandler.pages["/notes"] = _FixtureHandler.pages["/notes"].replace(
        "On-device perception keeps page text local.",
        "On-device perception now ships with a local planner.")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is not None
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    assert len(_events(database, mission)) == 1


def test_noise_filtering_also_applies_to_browser_observations(database):
    mission = _insert_mission(database, "https://example.org/notes")
    assert evaluate_observation(mission, {"text": "Report 2026-11-17 09:28, 5 minutes ago"}, observed_at=100, db_path=database) is None
    assert evaluate_observation(mission, {"text": "Report 2026-11-18 10:41, 2 minutes ago"}, observed_at=160, db_path=database) is None
    assert evaluate_observation(mission, {"text": "Report updated with new findings"}, observed_at=220, db_path=database) is not None
    assert len(_events(database, mission)) == 1


def test_http_url_gate_refuses_scheme_and_private_hosts(monkeypatch):
    assert _validate_monitor_url("file:///etc/passwd")[0] is False
    assert _validate_monitor_url("javascript:alert(1)")[0] is False
    assert _validate_monitor_url("https://user:pass@example.org/")[0] is False
    assert _validate_monitor_url("http://169.254.169.254/latest/meta-data/")[0] is False
    assert _validate_monitor_url("http://192.168.1.10/admin")[0] is False
    assert _validate_monitor_url("http://10.0.0.5/")[0] is False
    assert _validate_monitor_url("http://localhost:8000/")[0] is False
    # The 127.0.0.1 fixture address is the deliberate exception.
    assert _validate_monitor_url("http://127.0.0.1:8123/page")[0] is True
    # A public hostname passes when a proxy is configured (the proxy
    # resolves the host). Without one, this VM's DNS answers every
    # public name with a CGNAT address and the direct DNS check
    # correctly refuses it — so the public-host case is only
    # assertable under a proxy.
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    assert _validate_monitor_url("https://example.org/page")[0] is True


def test_http_redirect_to_private_host_is_refused(database, fixture_server):
    class RedirectHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises((ValueError, RuntimeError)):
            fetch_observation(f"http://127.0.0.1:{server.server_address[1]}/go")
    finally:
        server.shutdown()
        server.server_close()


def test_http_oversized_body_is_refused(fixture_server):
    _FixtureHandler.pages["/huge"] = "<html><body><p>" + ("x" * 1_100_000) + "</p></body></html>"
    with pytest.raises(ValueError, match="over"):
        fetch_observation(fixture_server + "/huge", max_bytes=1_048_576)


def test_scheduler_claim_run_now_and_http_path(database, fixture_server):
    # Run-now: a paused mission activated via the API is due immediately.
    mission = _insert_mission(database, fixture_server + "/notes", status="paused")
    client = TestClient(app)
    assert client.patch(f"/api/control/missions/{mission}", json={"status": "active"}).status_code == 200
    claimed = claim_due(now=time.time() + 1, db_path=database)
    assert [m["id"] for m in claimed] == [mission]
    assert claim_due(now=time.time() + 1, db_path=database) == []
    # No extension is connected in this VM, so execute_monitor takes
    # the HTTP path and baselines the page (no alert on first sight).
    assert hub.extension_ws is None
    asyncio.run(execute_monitor(claimed[0]))
    assert _events(database, mission) == []
    # Next scheduled run after a real content change alerts.
    _FixtureHandler.pages["/notes"] = _FixtureHandler.pages["/notes"].replace(
        "On-device perception keeps page text local.",
        "On-device perception now ships with a local planner.")
    with db.get_connection(database) as conn:
        row = dict(conn.execute("SELECT * FROM monitoring_missions WHERE id=?", (mission,)).fetchone())
    row["next_run_at"] = time.time() - 1
    asyncio.run(execute_monitor(row))
    events = _events(database, mission)
    assert len(events) == 1
    assert "local planner" in json.loads(events[0]["diff_data"])["after"]


def test_ui_list_endpoints_return_watch_and_alert(database, fixture_server):
    mission = _insert_mission(database, fixture_server + "/notes")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    _FixtureHandler.pages["/notes"] = _FixtureHandler.pages["/notes"].replace(
        "On-device perception keeps page text local.",
        "On-device perception now ships with a local planner.")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is not None
    client = TestClient(app)
    missions = client.get("/api/control/missions").json()
    assert any(m["id"] == mission and m["target_url"] == fixture_server + "/notes" for m in missions)
    events = client.get(f"/api/control/missions/{mission}/events").json()
    assert len(events) == 1 and events[0]["mission_id"] == mission
    # The Missions view renders exactly these two endpoints.
    assert "local planner" in events[0]["diff_data"]["after"]


def test_http_check_redacts_sensitive_text_before_storing(database, fixture_server):
    _FixtureHandler.pages["/notes"] = _FixtureHandler.pages["/notes"].replace(
        "On-device perception keeps page text local.",
        "Contact the owner at someone@example.org for details.")
    mission = _insert_mission(database, fixture_server + "/notes")
    assert check_monitor_http(mission, fixture_server + "/notes", db_path=database) is None
    with db.get_connection(database) as conn:
        state = conn.execute(
            "SELECT last_state_json FROM monitoring_missions WHERE id=?", (mission,)).fetchone()[0]
    assert "someone@example.org" not in state


def test_daily_price_digest_reports_every_run(database):
    """A daily_price mission reports the current price on EVERY run —
    including the first run (no baseline yet) and runs where the price
    has not changed — because it is a scheduled report, not a change
    alert. A page with no price reports nothing."""
    mission = _insert_mission(database, "https://example.org/fit3", condition="daily_price",
                              trigger="cron", cron="30 3 * * *", interval=86400)
    first = evaluate_observation(mission, {"text": "Samsung Galaxy Fit3 ₹3,899 only"},
                                 observed_at=100, db_path=database)
    assert first is not None
    assert "3899" in first["summary"]
    assert first["diff_data"]["new_price"] == 3899
    # Same price, unchanged page: a digest still reports.
    second = evaluate_observation(mission, {"text": "Samsung Galaxy Fit3 ₹3,899 only"},
                                  observed_at=200, db_path=database)
    assert second is not None
    assert len(_events(database, mission)) == 2
    # No price on the page: nothing to report.
    assert evaluate_observation(mission, {"text": "Samsung Galaxy Fit3 — out of stock"},
                                observed_at=300, db_path=database) is None


def test_below_price_fires_on_first_run_and_dedups(database):
    """Fix 6: a below-price threshold is a state, not a change — being
    under it alerts on the first run (the product may already be
    cheap), then deduplicates while the state is unchanged. A further
    drop alerts once more; at or above the threshold, nothing."""
    mission = _insert_mission(database, "https://example.org/halio-strap",
                              condition="below_price", params={"threshold": 5000})
    # Already below on the first run: alerts immediately.
    event = evaluate_observation(mission, {"text": "Halio strap ₹4,500"},
                                 observed_at=100, db_path=database)
    assert event is not None
    assert "below" in event["summary"].lower()
    assert event["diff_data"]["new_price"] == 4500
    # Same state again: no duplicate alert.
    assert evaluate_observation(mission, {"text": "Halio strap ₹4,500"},
                                observed_at=160, db_path=database) is None
    # Still below but cheaper: a new state, alerts once more.
    assert evaluate_observation(mission, {"text": "Halio strap ₹4,200"},
                                observed_at=220, db_path=database) is not None
    # Above the threshold: no alert.
    assert evaluate_observation(mission, {"text": "Halio strap ₹5,500"},
                                observed_at=280, db_path=database) is None
    # Exactly at the threshold: not below, no alert.
    assert evaluate_observation(mission, {"text": "Halio strap ₹5,000"},
                                observed_at=340, db_path=database) is None


# --- Backend flaw-hunt finding 4: one transient failure must not ---
# --- permanently kill a monitoring mission. -------------------------

def _mission_row(database, mission_id):
    with db.get_connection(database) as conn:
        return dict(conn.execute("SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone())


def test_one_transient_http_failure_keeps_watch_alive(database, monkeypatch):
    """A DNS blip during a check must not end the watch: the mission
    stays active, the failure count rises, and the next tick retries
    it. Previously the first failure set status='failed' and
    claim_due never selected it again."""
    mission = _insert_mission(database, "http://127.0.0.1:9/unreachable")
    row = _mission_row(database, mission)

    def _boom(*args, **kwargs):
        raise OSError("temporary failure in name resolution")

    monkeypatch.setattr("app.monitor_check.check_monitor_http", _boom)
    asyncio.run(execute_monitor(row))
    after = _mission_row(database, mission)
    assert after["status"] == "active"
    assert after["consecutive_failures"] == 1
    # Still due for the next tick.
    assert claim_due(now=time.time() + 3600, db_path=database, limit=5)


def test_five_consecutive_failures_end_watch_with_recorded_event(database, monkeypatch):
    """After MAX_CONSECUTIVE_MONITOR_FAILURES transient failures the
    watch does end — but the end is recorded as a monitoring event,
    so the user can see why it stopped instead of it vanishing."""
    mission = _insert_mission(database, "http://127.0.0.1:9/unreachable")
    def _boom(*args, **kwargs):
        raise OSError("temporary failure in name resolution")

    monkeypatch.setattr("app.monitor_check.check_monitor_http", _boom)
    for _ in range(4):
        asyncio.run(execute_monitor(_mission_row(database, mission)))
        assert _mission_row(database, mission)["status"] == "active"
    asyncio.run(execute_monitor(_mission_row(database, mission)))
    after = _mission_row(database, mission)
    assert after["status"] == "failed"
    assert after["consecutive_failures"] == 5
    events = _events(database, mission)
    assert len(events) == 1
    assert events[0]["title"] == "Monitoring stopped"
    assert "5 consecutive" in events[0]["summary"]


def test_permanent_url_refusal_still_ends_watch_immediately(database):
    """A structural refusal (private/internal host) is not transient:
    the watch ends on the first tick, with the reason recorded."""
    mission = _insert_mission(database, "http://localhost:9/notes")
    asyncio.run(execute_monitor(_mission_row(database, mission)))
    after = _mission_row(database, mission)
    assert after["status"] == "failed"
    assert "private" in _events(database, mission)[0]["summary"]


def test_successful_check_resets_failure_count(database, fixture_server):
    """A clean check after a failure resets the consecutive count,
    so isolated blips never accumulate toward the cap."""
    mission = _insert_mission(database, fixture_server + "/notes")
    with db.get_connection(database) as conn:
        conn.execute(
            "UPDATE monitoring_missions SET consecutive_failures=3 WHERE id=?", (mission,))
    asyncio.run(execute_monitor(_mission_row(database, mission)))
    after = _mission_row(database, mission)
    assert after["status"] == "active"
    assert after["consecutive_failures"] == 0
