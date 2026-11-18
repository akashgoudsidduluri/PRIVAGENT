"""Latency benchmark for the agent loop (Task 11).

Drives a fixed multi-step task — log in on a Moodle-style login page,
then search — against a scripted planner and a mocked extension, and
records per-step timings plus planner-call / observation-call counts.

Nothing here calls a live LLM, a live site, or Chrome. The scripted
planner emits the multi-step plans a real planner would (type username,
type password, click Log in; type query, click Search), with element
refs from the observation, so the benchmark measures the loop's own
overhead and round trips — not model quality.

The scripted planner also raises one ``ValueError`` (PLAN_INVALID)
the first time it sees the search page, to exercise the replan path:
the loop must re-observe after an invalid plan when the cache is cold,
and must NOT re-observe when the observation cache is warm.

``--planner-delay-ms`` simulates a real LLM call (each planner call
sleeps that long). It is a simulation, reported as such — the loop
overhead numbers are the measured part; the delay only shows how the
round-trip reduction converts into wall time against a slow planner.

Usage (from the repo root)::

    python backend/scripts/bench_loop.py [--planner-delay-ms 200] [--runs 3] [--out evals/results/latency_bench.json]

Output: a JSON summary on stdout (and to ``--out`` when given) with
planner_calls, observation_calls, actions_dispatched, steps, wall_ms
(median over runs), and the loop's own per-phase timing totals when
the loop reports them (``timings`` in the run result).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# Isolate the benchmark's SQLite writes: the loop checkpoints after
# every step, and those writes must not touch the operator's real
# backend/data/privagent.db. Set before importing app modules (the
# DB path is resolved at import time).
import tempfile

_BENCH_TMP = Path(tempfile.mkdtemp(prefix="privagent-bench-"))
os.environ["PRIVAGENT_DB_PATH"] = str(_BENCH_TMP / "bench.db")

from app import agentLoop  # noqa: E402


LOGIN_URL = "https://moodle.vce.ac.in/login/index.php"
SEARCH_URL = "https://moodle.vce.ac.in/"
RESULTS_URL = "https://moodle.vce.ac.in/search?q=mathematics"


def _el(target_id: str, ref: str, role: str, name: str, tag: str = "input",
         input_type: str | None = None) -> dict[str, Any]:
    el: dict[str, Any] = {
        "targetId": target_id,
        "elementRef": ref,
        "role": role,
        "name": name,
        "tag": tag,
        "visible": True,
        "enabled": True,
        "bounds": {"x": 100, "y": 100, "width": 200, "height": 30},
    }
    if input_type is not None:
        el["inputType"] = input_type
    return el


def _ax(ref: str, role: str, name: str, target_id: str, tag: str = "input",
        input_type: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "ref": ref,
        "role": role,
        "name": name,
        "tag": tag,
        "targetId": target_id,
        "cssSelector": f"#{target_id}",
        "xpath": f"//*[@id=\"{target_id}\"]",
        "disabled": False,
        "bounds": {"x": 100, "y": 100, "width": 200, "height": 30},
        "source": "dom-ax",
    }
    if input_type is not None:
        node["inputType"] = input_type
    return node


def _obs(url: str, title: str, elements: list[dict[str, Any]],
         ax_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tabId": 1,
        "url": url,
        "title": title,
        "interactive": elements,
        "axTree": ax_nodes,
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1,
    }


LOGIN_OBS = _obs(
    LOGIN_URL, "Log in to the site",
    [
        _el("username", "e0", "textbox", "Username", input_type="text"),
        _el("password", "e1", "textbox", "Password", input_type="password"),
        _el("loginbtn", "e2", "button", "Log in", tag="button"),
    ],
    [
        _ax("e0", "textbox", "Username", "username", input_type="text"),
        _ax("e1", "textbox", "Password", "password", input_type="password"),
        _ax("e2", "button", "Log in", "loginbtn", tag="button"),
    ],
)

SEARCH_OBS = _obs(
    SEARCH_URL, "e-Vidya | Vasavi College of Engineering(A)",
    [
        _el("search", "e0", "searchbox", "Search courses", input_type="search"),
        _el("searchbtn", "e1", "button", "Search courses", tag="button"),
    ],
    [
        _ax("e0", "searchbox", "Search courses", "search", input_type="search"),
        _ax("e1", "button", "Search courses", "searchbtn", tag="button"),
    ],
)

RESULTS_OBS = _obs(
    RESULTS_URL, "Search results: mathematics",
    [
        _el("c1", "e0", "link", "Mathematics I-SEM", tag="a"),
        _el("c2", "e1", "link", "Mathematics II-SEM", tag="a"),
    ],
    [
        _ax("e0", "link", "Mathematics I-SEM", "c1", tag="a"),
        _ax("e1", "link", "Mathematics II-SEM", "c2", tag="a"),
    ],
)


def _step(action: dict[str, Any], why: str) -> dict[str, Any]:
    return {"why": why, "action": action}


def _type_action(ref: str, name: str, target_id: str, text: str,
                 submit: bool = False) -> dict[str, Any]:
    return {
        "type": "type",
        "risk": "write",
        "verify": f"typed into {name}",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
        "params": {"text": text, "clear": True, "submit": submit},
    }


def _click_action(ref: str, name: str, target_id: str) -> dict[str, Any]:
    return {
        "type": "click",
        "risk": "write",
        "verify": f"clicked {name}",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
    }


class ScriptedPlanner:
    """Emits the plans a real planner would for this scripted task.

    Login page: type username, type password, click Log in (one plan,
    trimmed to what history shows is still undone). Search page: type
    query, click Search. The first plan on the search page is
    deliberately invalid (unknown ref e99) so the replan path — and
    the observation cache on it — is exercised. Results page: no
    steps (task complete).
    """

    def __init__(self, delay_ms: float = 0.0) -> None:
        self.calls = 0
        self.delay_ms = delay_ms
        self.invalid_raised = False

    @staticmethod
    def _typed(history: list[dict[str, Any]], target_id: str) -> bool:
        for entry in history:
            action = entry.get("action") or {}
            if (
                entry.get("result_ok")
                and action.get("type") == "type"
                and (action.get("target") or {}).get("targetId") == target_id
            ):
                return True
        return False

    def __call__(self, instruction: str, payload: Any,
                 history: list[dict[str, Any]],
                 working_memory: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000.0)
        url = getattr(payload, "url", "") or ""
        if url == LOGIN_URL:
            steps: list[dict[str, Any]] = []
            if not self._typed(history, "username"):
                steps.append(_step(_type_action("e0", "Username", "username", "student"), "fill username"))
            if not self._typed(history, "password"):
                steps.append(_step(_type_action("e1", "Password", "password", "hunter2"), "fill password"))
            steps.append(_step(_click_action("e2", "Log in", "loginbtn"), "submit the login form"))
            return {"goal": instruction, "steps": steps}
        if url == SEARCH_URL:
            if not self.invalid_raised:
                self.invalid_raised = True
                raise ValueError("PLAN_INVALID: unknown element ref 'e99'")
            steps = []
            if not self._typed(history, "search"):
                steps.append(_step(_type_action("e0", "Search courses", "search", "mathematics"), "type the query"))
            steps.append(_step(_click_action("e1", "Search courses", "searchbtn"), "run the search"))
            return {"goal": instruction, "steps": steps}
        return {"goal": instruction, "steps": []}


class FakeExtension:
    """Mocked extension: applies actions to a simulated page state.

    A type stays on the same page; clicking Log in lands on the
    search page; clicking Search lands on the results page. Every
    successful action returns the post-action observation, like the
    real extension does.
    """

    def __init__(self) -> None:
        self.current = LOGIN_OBS
        self.actions_dispatched = 0

    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.actions_dispatched += 1
        atype = action.get("type")
        target = action.get("target") or {}
        if atype == "type":
            return {"ok": True, "evidence": f"typed into {target.get('name')}",
                    "observation": self.current}
        if atype == "click" and target.get("targetId") == "loginbtn":
            self.current = SEARCH_OBS
            return {"ok": True, "evidence": "logged in",
                    "observation": self.current}
        if atype == "click" and target.get("targetId") == "searchbtn":
            self.current = RESULTS_OBS
            return {"ok": True, "evidence": "searched",
                    "observation": self.current}
        return {"ok": True, "evidence": f"{atype} ok", "observation": self.current}


def run_once(delay_ms: float) -> dict[str, Any]:
    planner = ScriptedPlanner(delay_ms=delay_ms)
    ext = FakeExtension()
    obs_calls = 0

    async def obs_factory() -> dict[str, Any]:
        nonlocal obs_calls
        obs_calls += 1
        return ext.current

    started = time.perf_counter()
    result = asyncio.run(agentLoop.run_with_injection(
        ext,
        planner=planner,
        obs_factory=obs_factory,
        task_id="bench-loop",
        instruction="Log in as student and search for mathematics",
    ))
    wall_ms = (time.perf_counter() - started) * 1000.0
    return {
        "final": result.get("final"),
        "steps": result.get("steps"),
        "planner_calls": planner.calls,
        "observation_calls": obs_calls,
        "actions_dispatched": ext.actions_dispatched,
        "wall_ms": round(wall_ms, 2),
        "timings": result.get("timings"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planner-delay-ms", type=float, default=0.0,
                        help="simulated per-planner-call latency (ms)")
    parser.add_argument("--runs", type=int, default=3,
                        help="repeat the task this many times; report medians")
    parser.add_argument("--out", type=str, default="",
                        help="write the JSON summary to this path")
    args = parser.parse_args()

    runs = [run_once(args.planner_delay_ms) for _ in range(args.runs)]
    for i, r in enumerate(runs):
        if r["final"] != "completed":
            print(f"run {i}: task did NOT complete: {r['final']} "
                  f"(steps={r['steps']})", file=sys.stderr)
            return 1
    if len({(r["planner_calls"], r["observation_calls"], r["actions_dispatched"], r["steps"])
            for r in runs}) != 1:
        print("runs disagree on counts — benchmark is not deterministic:",
              runs, file=sys.stderr)
        return 1

    first = runs[0]
    summary = {
        "task": "log in (type username, type password, click Log in) + "
                "search (type query, click Search)",
        "runs": args.runs,
        "planner_delay_ms": args.planner_delay_ms,
        "planner_calls": first["planner_calls"],
        "observation_calls": first["observation_calls"],
        "actions_dispatched": first["actions_dispatched"],
        "steps": first["steps"],
        "wall_ms_median": round(statistics.median(r["wall_ms"] for r in runs), 2),
        "wall_ms_runs": [r["wall_ms"] for r in runs],
        "timings": first["timings"],
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
