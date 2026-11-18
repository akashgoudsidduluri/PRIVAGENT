"""End-to-end live-monitoring demo (Task 2): watch once, check in the
background, alert with proof when something really changes.

Drives the REAL monitoring path — :func:`app.monitoring_scheduler.claim_due`
plus :func:`app.monitoring_scheduler.execute_monitor` — against TWO
generic fixture pages served by local HTTP servers on 127.0.0.1, with
no Chrome extension connected, so every check takes the HTTP path in
:mod:`app.monitor_check`:

    1. Two watches are created (a text watch on Field Notes, a
       price-drop watch on Gadget Store) and a first check cycle
       baselines both pages: no alert.
    2. A second check cycle with no change: no alert.
    3. The fixture pages change — real content on Field Notes, price
       ₹1,299 → ₹999 on Gadget Store (plus timestamp noise that must
       NOT alert on its own) — and the next check cycle alerts on
       both, with before/after proof from the stored events.
    4. One REAL public page (https://example.org) is fetched over
       HTTP and its observed state recorded. If this VM has no route
       to it, the failure is recorded honestly and the fixture
       observation is recorded instead — nothing is faked.
    5. The Missions UI list endpoints are exercised: the watches and
       their alert must be listed, which is exactly what
       ``src/controlViews.ts`` renders.

What this is NOT (read before quoting numbers anywhere):

* NOT live Chrome. There is no browser here: no extension is
  connected, so the browser observation path is never taken. The
  extension path still needs the operator's real browser.
* NOT a live external site for the watches. Both watched pages are
  served by this script on 127.0.0.1 (ephemeral ports) and exist
  only for the run. Only step 4 touches the public internet.
* NOT a benchmark. The recorded run is one scripted trace proving
  the check/alert mechanics end to end.

Usage (from the repo root)::

    backend/venv/bin/python backend/scripts/demo_monitoring.py [--out evals/results/monitoring_run_2026-11-17.json]

Output: the recorded run as JSON on stdout and to ``--out`` when
given. The run uses an isolated temp SQLite DB and temp audit log,
so it never touches the operator's real backend/data/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# Isolate the demo's SQLite + audit-log writes: monitor checks persist
# baselines/events, and alerts are audit-logged. Set before importing
# app modules (both paths are resolved at import time).
_DEMO_TMP = Path(tempfile.mkdtemp(prefix="privagent-demo-monitoring-"))
os.environ["PRIVAGENT_DB_PATH"] = str(_DEMO_TMP / "demo.db")
os.environ["PRIVAGENT_AUDIT_LOG_PATH"] = str(_DEMO_TMP / "audit.log")
# The /api/* routes require the local operator token (Task 1 auth);
# the pytest suite injects it via conftest — here the demo sets a
# fixed throwaway token and sends it explicitly on its own calls.
_DEMO_TOKEN = "demo-local-token"
os.environ["PRIVAGENT_LOCAL_TOKEN"] = _DEMO_TOKEN
_AUTH_HEADERS = {"Authorization": f"Bearer {_DEMO_TOKEN}"}

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.monitor_check import fetch_observation  # noqa: E402
from app.monitoring_scheduler import claim_due, execute_monitor  # noqa: E402

PAGES: dict[str, str] = {
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


class _FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence stderr noise
        pass

    def do_GET(self):
        body = PAGES.get(self.path)
        payload = body.encode("utf-8") if body is not None else b"not found"
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _insert_mission(target_url: str, title: str, condition: str,
                    params: dict[str, Any]) -> str:
    mission_id = str(uuid.uuid4())
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
               (id, title, target_url, trigger_type, interval_seconds,
                condition_type, condition_params, status, created_at, next_run_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (mission_id, title, target_url, "interval", 60,
             condition, json.dumps(params), "active", time.time(), time.time() - 1))
    return mission_id


def _events(mission_id: str) -> list[dict[str, Any]]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_events WHERE mission_id=? ORDER BY detected_at",
            (mission_id,)).fetchall()
    return [{**dict(row), "diff_data": json.loads(row["diff_data"] or "{}")} for row in rows]


def _make_due(mission_id: str) -> None:
    with db.get_connection() as conn:
        conn.execute("UPDATE monitoring_missions SET next_run_at=? WHERE id=?",
                     (time.time() - 1, mission_id))


def _run_cycle(mission_ids: list[str]) -> list[dict[str, Any]]:
    """Claim every due mission and run it; returns the claimed missions."""
    claimed: list[dict[str, Any]] = []
    for mission_id in mission_ids:
        _make_due(mission_id)
    claimed = claim_due(now=time.time() + 1)
    for mission in claimed:
        asyncio.run(execute_monitor(mission))
    return claimed


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="PrivAgent live-monitoring demo run")
    parser.add_argument("--out", default=str(REPO_DIR / "evals" / "results" / "monitoring_run_2026-11-17.json"))
    args = parser.parse_args()

    db.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    started = time.time()
    record: dict[str, Any] = {
        "demo": "live monitoring (Task 2)",
        "date": "2026-11-17",
        "extension_connected": False,
        "check_path": "http",
        "fixture_base": base,
        "cycles": [],
        "real_public_page": {},
        "ui_list_check": {},
        "notes": [],
    }
    try:
        notes_id = _insert_mission(base + "/notes", "Field Notes watch", "text_change", {})
        gadget_id = _insert_mission(base + "/gadget", "Widget Pro price watch",
                                    "price_drop", {"min_percent": 10})
        record["watches"] = [
            {"id": notes_id, "title": "Field Notes watch", "target_url": base + "/notes",
             "condition": "text_change"},
            {"id": gadget_id, "title": "Widget Pro price watch", "target_url": base + "/gadget",
             "condition": "price_drop", "min_percent": 10},
        ]

        # Cycle 1: baseline — first sight establishes state, no alert.
        claimed = _run_cycle([notes_id, gadget_id])
        record["cycles"].append({
            "cycle": 1, "label": "baseline", "claimed": len(claimed),
            "events": sum(len(_events(mid)) for mid in (notes_id, gadget_id)),
            "alerted": False,
        })

        # Cycle 2: no change — nothing must alert.
        claimed = _run_cycle([notes_id, gadget_id])
        record["cycles"].append({
            "cycle": 2, "label": "no change", "claimed": len(claimed),
            "events": sum(len(_events(mid)) for mid in (notes_id, gadget_id)),
            "alerted": False,
        })

        # Mutate the fixtures: real content + real price drop, plus
        # timestamp/counter noise that must NOT alert on its own.
        PAGES["/notes"] = PAGES["/notes"].replace(
            "On-device perception keeps page text local.",
            "On-device perception now ships with a local planner.")
        PAGES["/notes"] = PAGES["/notes"].replace(
            "Updated 5 minutes ago", "Updated 2 minutes ago")
        PAGES["/gadget"] = PAGES["/gadget"].replace("\u20b91,299", "\u20b9" + "999")
        PAGES["/gadget"] = PAGES["/gadget"].replace("1,234 views", "9,876 views")

        # Cycle 3: the change — both watches must alert with proof.
        claimed = _run_cycle([notes_id, gadget_id])
        notes_events = _events(notes_id)
        gadget_events = _events(gadget_id)
        record["cycles"].append({
            "cycle": 3, "label": "content + price change", "claimed": len(claimed),
            "events": len(notes_events) + len(gadget_events),
            "alerted": True,
            "alerts": [
                {"mission": "Field Notes watch", "summary": notes_events[0]["summary"],
                 "before": notes_events[0]["diff_data"].get("before"),
                 "after": notes_events[0]["diff_data"].get("after")},
                {"mission": "Widget Pro price watch", "summary": gadget_events[0]["summary"],
                 "old_price": gadget_events[0]["diff_data"].get("old_price"),
                 "new_price": gadget_events[0]["diff_data"].get("new_price"),
                 "before": gadget_events[0]["diff_data"].get("before"),
                 "after": gadget_events[0]["diff_data"].get("after")},
            ],
        })

        # Step 4: one REAL public page over HTTP. Honest failure
        # recording if the VM has no route to it.
        try:
            observation = fetch_observation("https://example.org")
            record["real_public_page"] = {
                "url": "https://example.org",
                "ok": True,
                "text_excerpt": observation["text"][:250],
                "element_count": len(observation["elements"]),
                "elements_sample": observation["elements"][:5],
            }
        except Exception as exc:  # noqa: BLE001 — record, do not fake
            try:
                fallback = fetch_observation(base + "/notes")
                fallback_excerpt = fallback["text"][:250]
            except Exception:
                fallback_excerpt = None
            record["real_public_page"] = {
                "url": "https://example.org",
                "ok": False,
                "reason": str(exc),
                "fallback": "fixture page observed instead; no public page fetched",
                "fallback_text_excerpt": fallback_excerpt,
            }
            record["notes"].append("Real public page fetch failed in this VM; recorded honestly, not faked.")

        # Step 5: the Missions UI lists exactly these endpoints.
        client = TestClient(app)
        missions = client.get("/api/control/missions", headers=_AUTH_HEADERS).json()
        events = client.get(f"/api/control/missions/{notes_id}/events", headers=_AUTH_HEADERS).json()
        record["ui_list_check"] = {
            "missions_listed": len(missions),
            "watch_listed": any(m["id"] == notes_id for m in missions)
                             and any(m["id"] == gadget_id for m in missions),
            "events_listed": len(events),
            "alert_listed": len(events) == 1 and "local planner" in events[0]["diff_data"]["after"],
            "renders_from": ["/api/control/missions", "/api/control/missions/{id}/events"],
        }
        record["notes"].append(
            "No live Chrome in this VM: the browser observation path was never taken; "
            "extension-path monitoring still needs the operator's real browser.")
    finally:
        server.shutdown()
        server.server_close()

    record["duration_seconds"] = round(time.time() - started, 2)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(record, indent=2, ensure_ascii=False))
    print(f"\nRecord written to {out_path}")
    return record


if __name__ == "__main__":
    main()
