"""End-to-end automation demo (Task 1): "research across 2 sites and
summarise".

Drives the REAL agent loop (:func:`app.agentLoop.run_with_injection`
— the same ``_run_loop`` the production ``run_task`` entrypoint
executes, with the planner, extension hub and observation source
injected) with a scripted planner and a fixture extension stub
against TWO generic fixture sites served by local HTTP servers on
127.0.0.1. The run exercises the full loop on each step:

    plan -> observe -> act by elementRef -> verify outcome

and it exercises the Task 1 recovery path: mid-run, the fixture
re-renders the library page and moves the "Summary" link to a new
element ref / target id, so the first click attempt fails with
STALE_TARGET and the loop re-observes, resolves the fresh ref and
retries once — then completes.

What this is NOT (read before quoting numbers anywhere):

* NOT live Chrome. There is no browser here: the "extension" is a
  Python stub implementing the hub's ``send_action`` contract, and
  the "planner" is a deterministic script that grounds its refs from
  the observation (like a real planner would) instead of calling an
  LLM. Timings are the loop's own overhead on this machine, not
  browser timings.
* NOT a live external site. Both fixture sites are served by this
  script on 127.0.0.1 (ephemeral ports) and exist only for the run;
  no network leaves the machine. No real site is hardcoded anywhere
  — the planner finds its targets by accessible name in whatever
  observation it is given.
* NOT a benchmark. The recorded run is one scripted trace proving
  the loop mechanics (verification + recovery) end to end; latency
  is measured by ``backend/scripts/bench_loop.py`` instead.

Usage (from the repo root)::

    backend/venv/bin/python backend/scripts/demo_automation.py [--out evals/results/automation_run_2026-11-17.json]

Output: the recorded run (steps, refs used, recovery events,
per-step timings, planner/observation counts) as JSON on stdout and
to ``--out`` when given.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# Isolate the demo's SQLite writes: the loop checkpoints after every
# step, and those writes must not touch the operator's real
# backend/data/privagent.db. Set before importing app modules (the
# DB path is resolved at import time).
import tempfile

_DEMO_TMP = Path(tempfile.mkdtemp(prefix="privagent-demo-automation-"))
os.environ["PRIVAGENT_DB_PATH"] = str(_DEMO_TMP / "demo.db")

from app import agentLoop  # noqa: E402

INSTRUCTION = (
    "Research across 2 sites and summarise: read the field notes on "
    "the Research Hub, then read the summary on the Reference Library, "
    "and summarise what on-device perception means for a light-weight "
    "browser agent."
)

# ---------------------------------------------------------------------------
# Fixture sites (single source of truth: the HTTP handler renders these
# pages, and the stub builds observations from the same definitions).
# ---------------------------------------------------------------------------

# Page model: path -> {title, text, links: [{name, target_id, href_key}]}
# ``href_key`` is resolved to a URL once the servers know their ports.
_HUB_PAGES: dict[str, dict[str, Any]] = {
    "/hub": {
        "title": "Research Hub",
        "text": (
            "Research Hub — field notes and references for light-weight "
            "browser agents. Two items on this page: field notes on "
            "light-weight agents, and a link to the Reference Library."
        ),
        "elements": [
            {"role": "searchbox", "name": "Search the hub", "target_id": "hub-search", "tag": "input", "input_type": "search"},
            {"role": "link", "name": "Field notes on light-weight agents", "target_id": "article-link", "tag": "a", "href_key": "article"},
            {"role": "link", "name": "Reference library", "target_id": "library-link", "tag": "a", "href_key": "library"},
        ],
    },
    "/article": {
        "title": "Field notes on light-weight agents",
        "text": (
            "Field notes on light-weight agents. A light-weight browser "
            "agent must do real work — navigate, read, compare — without "
            "sending the page to a server. Perception runs on the device: "
            "the accessibility tree names every control, so the agent "
            "acts by element reference instead of raw coordinates. "
            "Fact one: on-device grounding keeps interaction private. "
            "Fact two: verification after every step is what makes a "
            "multi-step job trustworthy instead of a silent no-op."
        ),
        "elements": [
            {"role": "link", "name": "Back to the hub", "target_id": "hub-link", "tag": "a", "href_key": "hub"},
            {"role": "link", "name": "Open the reference library", "target_id": "library-link", "tag": "a", "href_key": "library"},
        ],
    },
}

_LIBRARY_PAGES: dict[str, dict[str, Any]] = {
    "/library": {
        "title": "Reference Library",
        "text": (
            "Reference Library — one summary on this page: on-device "
            "perception for a light-weight browser agent."
        ),
        "elements": [
            {"role": "link", "name": "Summary: on-device perception", "target_id": "summary-link", "tag": "a", "href_key": "summary"},
        ],
    },
    "/library/summary": {
        "title": "Summary: on-device perception",
        "text": (
            "Summary: on-device perception. On-device perception means "
            "the agent reads the page where the page lives: element "
            "detection and text extraction run locally, sensitive data "
            "is redacted before anything reaches a model, and every "
            "action is verified against the live page afterwards."
        ),
        "elements": [
            {"role": "link", "name": "Back to the library", "target_id": "library-link", "tag": "a", "href_key": "library"},
        ],
    },
}


def _render_html(title: str, text: str, elements: list[dict[str, Any]], urls: dict[str, str]) -> str:
    links = "".join(
        f'<p><a href="{urls[e["href_key"]]}">{e["name"]}</a></p>'
        for e in elements if e["role"] == "link"
    )
    search = "".join(
        f'<p><input type="search" aria-label="{e["name"]}"></p>'
        for e in elements if e["role"] == "searchbox"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><h1>{title}</h1>"
        f"<p>{text}</p>{search}{links}</body></html>"
    )


def _make_handler(pages: dict[str, dict[str, Any]], urls: dict[str, str]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep stdout clean
            pass

        def do_GET(self) -> None:
            page = pages.get(self.path)
            if page is None:
                self.send_response(404)
                self.end_headers()
                return
            body = _render_html(page["title"], page["text"], page["elements"], urls).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class FixtureSites:
    """Two local HTTP servers (one per fixture site) + URL map."""

    def __init__(self) -> None:
        self.servers: list[ThreadingHTTPServer] = []
        self.urls: dict[str, str] = {}
        # Ports are assigned first (ephemeral), then the URL map is
        # built, then the servers start serving it.
        hub_server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(_HUB_PAGES, self.urls))
        lib_server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(_LIBRARY_PAGES, self.urls))
        hub_port = hub_server.server_address[1]
        lib_port = lib_server.server_address[1]
        self.urls.update({
            "hub": f"http://127.0.0.1:{hub_port}/hub",
            "article": f"http://127.0.0.1:{hub_port}/article",
            "library": f"http://127.0.0.1:{lib_port}/library",
            "summary": f"http://127.0.0.1:{lib_port}/library/summary",
        })
        self.servers = [hub_server, lib_server]
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in self.servers]

    def __enter__(self) -> "FixtureSites":
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        for s in self.servers:
            s.shutdown()
            s.server_close()


# ---------------------------------------------------------------------------
# Fixture extension stub
# ---------------------------------------------------------------------------

class FixtureHub:
    """Implements the extension hub's send_action contract.

    State: the "browser" is on one fixture URL; navigate / link clicks
    move it. The mid-run element move is injected here: the first
    click on the library's "Summary" link fails with STALE_TARGET
    (recoverable) — the page re-rendered and the link moved to a new
    ref / target id — and only a retry against the fresh observation
    succeeds.
    """

    def __init__(self, sites: FixtureSites) -> None:
        self.sites = sites
        self.current_url = sites.urls["hub"]
        self.dispatched: list[dict[str, Any]] = []
        self.summary_moved = False

    # -- observation ----------------------------------------------------
    def _page_for(self, url: str) -> tuple[str, dict[str, Any]]:
        for key, page_url in self.sites.urls.items():
            if url == page_url:
                pages = _HUB_PAGES if key in ("hub", "article") else _LIBRARY_PAGES
                path = "/" + key if key != "summary" else "/library/summary"
                return key, pages[path]
        return "hub", _HUB_PAGES["/hub"]

    def observation(self) -> dict[str, Any]:
        key, page = self._page_for(self.current_url)
        raw_elements = list(page["elements"])
        if key == "library" and self.summary_moved:
            # Mid-run re-render: a "Back to the hub" link appears above
            # the Summary link, so the Summary link shifts to a new
            # ref (e0 -> e1) AND a new target id (summary-link ->
            # summary-link-v2). The fresh observation is the only
            # thing that can ground the retry.
            raw_elements = [
                {"role": "link", "name": "Back to the hub", "target_id": "hub-link", "tag": "a", "href_key": "hub"},
                {"role": "link", "name": "Summary: on-device perception", "target_id": "summary-link-v2", "tag": "a", "href_key": "summary"},
            ]
        elements = []
        for idx, el in enumerate(raw_elements):
            target_id = el["target_id"]
            elements.append({
                "targetId": target_id,
                "elementRef": f"e{idx}",
                "role": el["role"],
                "name": el["name"],
                "tag": el["tag"],
                "visible": True,
                "enabled": True,
                "bounds": {"x": 100, "y": 100 + 40 * idx, "width": 300, "height": 30},
                **({"inputType": el["input_type"]} if "input_type" in el else {}),
            })
        return {
            "tabId": 1,
            "url": self.current_url,
            "title": page["title"],
            "page_text": page["text"],
            "interactive": elements,
            "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
            "dpr": 1,
        }

    # -- actions ---------------------------------------------------------
    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.dispatched.append(dict(action))
        atype = action.get("type")
        if atype == "navigate":
            self.current_url = (action.get("params") or {}).get("url", self.current_url)
            return {"ok": True, "evidence": f"navigated to {self.current_url}",
                    "observation": self.observation()}
        if atype == "click":
            target = action.get("target") or {}
            target_id = str(target.get("targetId") or "")
            if target_id == "article-link":
                self.current_url = self.sites.urls["article"]
                return {"ok": True, "evidence": "opened field notes",
                        "observation": self.observation()}
            if target_id == "library-link":
                self.current_url = self.sites.urls["library"]
                return {"ok": True, "evidence": "opened reference library",
                        "observation": self.observation()}
            if target_id in ("summary-link", "summary-link-v2"):
                if target_id == "summary-link":
                    # Mid-run element move: the link re-rendered under
                    # a new ref / target id. A click aimed at the old
                    # id can NEVER succeed again — not on the first
                    # attempt, and not on an identical retry — so it
                    # fails every time until the loop re-observes and
                    # retries with the fresh ref.
                    self.summary_moved = True
                    return {"ok": False, "error": {
                        "code": "STALE_TARGET",
                        "message": "no matching element for Summary link (page re-rendered)",
                        "recoverable": True,
                    }}
                self.current_url = self.sites.urls["summary"]
                return {"ok": True, "evidence": "opened summary",
                        "observation": self.observation()}
            if target_id == "hub-link":
                self.current_url = self.sites.urls["hub"]
                return {"ok": True, "evidence": "back to hub",
                        "observation": self.observation()}
            return {"ok": False, "error": {
                "code": "STALE_TARGET", "message": f"no matching element for {target_id}",
                "recoverable": True,
            }}
        if atype == "type":
            return {"ok": True, "evidence": "typed", "observation": self.observation()}
        return {"ok": True, "evidence": "ok", "observation": self.observation()}


# ---------------------------------------------------------------------------
# Scripted planner (grounds refs from the observation it is given)
# ---------------------------------------------------------------------------

def _payload_field(payload: Any, name: str) -> Any:
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


def _el_field(el: Any, name: str) -> Any:
    if isinstance(el, dict):
        return el.get(name)
    return getattr(el, {
        "targetId": "target_id",
        "elementRef": "element_ref",
    }.get(name, name), None)


def _find_ref(payload: Any, name: str) -> dict[str, Any] | None:
    # Refs live in the AX tree (payload.ax_tree), not on the
    # interactive elements — the planner quotes the ref the tree
    # assigned, exactly as a real planner reading the rendered tree
    # would. Fall back to the interactive list for observations that
    # carry no tree.
    for el in _payload_field(payload, "ax_tree") or []:
        if _el_field(el, "name") == name:
            return el
    for el in _payload_field(payload, "interactive") or []:
        if _el_field(el, "name") == name:
            return el
    return None


def _click_step(payload: Any, name: str, why: str) -> dict[str, Any]:
    el = _find_ref(payload, name)
    if el is None:
        raise ValueError(f"scripted planner: no element named {name!r} in the observation")
    ref = _el_field(el, "ref") or _el_field(el, "elementRef")
    return {"why": why, "action": {
        "type": "click", "risk": "write", "verify": f"clicked {name}",
        "timeoutMs": 5000,
        "target": {"targetId": _el_field(el, "targetId"), "name": name, "elementRef": ref},
    }}


class ScriptedPlanner:
    """Deterministic planner for the demo route.

    Hub -> field notes -> reference library -> summary -> answer.
    It never hardcodes a ref: every step resolves its target from the
    observation the loop hands it, exactly as a real planner quoting
    element refs would.
    """

    def __init__(self, sites: FixtureSites) -> None:
        self.sites = sites
        self.calls = 0

    def __call__(self, instruction: str, payload: Any,
                 history: list[dict[str, Any]], working_memory: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        url = str(_payload_field(payload, "url") or "")
        if url == self.sites.urls["hub"]:
            return {"goal": instruction, "steps": [
                _click_step(payload, "Field notes on light-weight agents",
                            "On the hub: opening the field notes"),
            ]}
        if url == self.sites.urls["article"]:
            return {"goal": instruction, "steps": [
                _click_step(payload, "Open the reference library",
                            "Notes read: moving to the second site"),
            ]}
        if url == self.sites.urls["library"]:
            return {"goal": instruction, "steps": [
                _click_step(payload, "Summary: on-device perception",
                            "On the library: opening the summary"),
            ]}
        if url == self.sites.urls["summary"]:
            return {"goal": instruction, "steps": [], "answer": (
                "Summary across 2 sites. Field notes (Research Hub): a light-weight "
                "browser agent must do real work without sending the page to a server; "
                "on-device grounding keeps interaction private, and verification after "
                "every step is what makes a multi-step job trustworthy. Summary "
                "(Reference Library): on-device perception means the agent reads the "
                "page where the page lives — element detection and text extraction run "
                "locally, sensitive data is redacted before reaching a model, and every "
                "action is verified against the live page afterwards."
            )}
        raise ValueError(f"scripted planner: unexpected page {url!r}")


# ---------------------------------------------------------------------------
# Run + record
# ---------------------------------------------------------------------------

def run_demo(out_path: str | Path | None = None) -> dict[str, Any]:
    started = time.monotonic()
    with FixtureSites() as sites:
        hub = FixtureHub(sites)
        planner = ScriptedPlanner(sites)
        obs_calls = {"n": 0}

        def obs_factory() -> dict[str, Any]:
            obs_calls["n"] += 1
            return hub.observation()

        result = asyncio.run(agentLoop.run_with_injection(
            hub, planner=planner, obs_factory=obs_factory,
            task_id="automation-demo-2026-11-17",
            instruction=INSTRUCTION,
        ))
    wall_ms = (time.monotonic() - started) * 1000.0

    steps = []
    for idx, entry in enumerate(result.get("history") or [], start=1):
        action = entry.get("action") or {}
        target = action.get("target") or {}
        outcome = entry.get("outcome") or {}
        steps.append({
            "step": idx,
            "action": action.get("type"),
            "target_name": target.get("name"),
            "ref_used": target.get("elementRef") or target.get("element_ref"),
            "target_id": target.get("targetId"),
            "result_ok": bool(entry.get("result_ok")),
            "status": entry.get("status"),
            "outcome_verified": bool(outcome.get("verified")),
            "outcome_reason": outcome.get("reason"),
            "recovery": entry.get("recovery"),
        })
    timings = result.get("timings") or {}
    record = {
        "task": "automation-demo",
        "instruction": INSTRUCTION,
        "date": "2026-11-17",
        "final": result.get("final"),
        "steps_done": result.get("steps"),
        "summary": result.get("summary"),
        "provenance": {
            "loop": "app.agentLoop.run_with_injection (the real _run_loop; production run_task executes the same loop)",
            "planner": "scripted deterministic planner (grounds refs from each observation; NOT a live LLM)",
            "extension": "fixture extension stub implementing the hub send_action contract (NOT live Chrome)",
            "sites": "two fixture sites served by this script on 127.0.0.1 (ephemeral ports); no live external site",
            "live_chrome": False,
        },
        "sites": sites.urls,
        "counts": {
            "planner_calls": planner.calls,
            "observation_calls": obs_calls["n"],
            "actions_dispatched": len(hub.dispatched),
            "steps": result.get("steps"),
        },
        "wall_ms": round(wall_ms, 1),
        "steps": steps,
        "recoveries": (result.get("working_memory") or {}).get("automation_recoveries", []),
        "per_step_timings": timings.get("steps", []),
        "timings_summary": {
            "steps_executed": timings.get("steps_executed"),
            "batched_steps": timings.get("batched_steps"),
            "planner_calls": timings.get("planner_calls"),
            "observation_calls": timings.get("observation_calls"),
        },
    }
    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=2), encoding="utf-8")
        record["output_path"] = str(out)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(REPO_DIR / "evals" / "results" / "automation_run_2026-11-17.json"),
        help="where to write the recorded run (JSON)",
    )
    args = parser.parse_args()
    record = run_demo(args.out)
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
