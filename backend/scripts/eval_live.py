"""Measured eval harness for PrivAgent (Task 6).

Runs the three measurements that are possible offline, with the mock
planner, and writes machine-readable results plus redacted trajectories:

* ``checker`` — Task 3 web-blind checker ASR over
  ``backend/tests/fixtures/injection_cases.json``.  Attack success =
  the plan that follows the injected instruction is NOT flagged.
  Both the scan-level detection rate and the plan-level flag rate are
  reported, because they differ (see docs/EVALS.md).
* ``privacy`` — redaction leak rate over
  ``backend/tests/fixtures/pii_cases.json``.  A leak = the exact
  synthetic PII token survives ``classify_and_redact`` +
  ``build_prompt`` into the planner prompt.
* ``mock`` — the existing deterministic fixture benchmark A-H
  (``backend/scripts/benchmark_eval.py``) with per-scenario
  trajectories recorded.  Mock fixtures only, never live metrics.

Nothing here calls a live LLM, a live site, or Chrome.  ``--provider``
accepts ``mock`` only; any other value fails with the list of env vars
a real run needs (see ``docs/EVALS.md`` — WebArena-Verified, OSWorld
2.0, PrivacyLens and AgentDojo are harness-ready, run pending).

Usage (from the repo root)::

    python backend/scripts/eval_live.py --suite all
    python backend/scripts/eval_live.py --suite checker --out-dir evals

Outputs (under ``--out-dir``, default ``evals/``)::

    results/checker_asr_2026-11-16.json
    results/privacy_leak_2026-11-16.json
    results/mock_benchmark_2026-11-16.json
    trajectories/mock_benchmark_2026-11-16.jsonl
    results/summary_2026-11-16.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.checker import check_plan, scan_page_text  # noqa: E402
from app.contracts import BrowserObservation, InteractiveElement  # noqa: E402
from app.planner import build_prompt  # noqa: E402
from app.privacy import classify_and_redact  # noqa: E402

REAL_PROVIDER_ENV = ("GEMINI_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


def _load_fixture(name: str) -> dict:
    path = BACKEND_DIR / "tests" / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _steps(actions: list[dict]) -> list[dict]:
    return [{"why": "eval", "action": a} for a in actions]


# ---------------------------------------------------------------------------
# Checker ASR
# ---------------------------------------------------------------------------

def run_checker_asr() -> dict:
    fixture = _load_fixture("injection_cases.json")
    instruction = fixture["instruction"]
    current_url = fixture["current_url"]
    cases = fixture["cases"]
    rows = []
    for case in cases:
        findings = scan_page_text(case["page_text"])
        found_rules = {f.rule for f in findings}
        result = check_plan(
            instruction,
            _steps(case["plan"]),
            findings,
            current_url=current_url,
        )
        rows.append({
            "id": case["id"],
            "kind": case["kind"],
            "family": case["family"],
            "expected_rule": case["expected_rule"],
            "scan_detected": case["expected_rule"] in found_rules if case["expected_rule"] else False,
            "scan_rules": sorted(found_rules),
            "plan_flagged": result.flagged,
            "reasons": result.reasons,
        })

    def _rate(subset: list[dict], key: str) -> float | None:
        if not subset:
            return None
        return round(sum(1 for r in subset if r[key]) / len(subset), 4)

    by_family: dict[str, dict] = {}
    families = sorted({r["family"] for r in rows if r["family"]})
    for fam in families:
        sub = [r for r in rows if r["family"] == fam]
        can = [r for r in sub if r["kind"] == "canonical"]
        ev = [r for r in sub if r["kind"] == "evasion"]
        by_family[fam] = {
            "n": len(sub),
            "canonical_n": len(can),
            "canonical_scan_detection_rate": _rate(can, "scan_detected"),
            "canonical_plan_flag_rate": _rate(can, "plan_flagged"),
            "evasion_n": len(ev),
            "evasion_scan_detection_rate": _rate(ev, "scan_detected"),
            "evasion_plan_flag_rate": _rate(ev, "plan_flagged"),
        }

    canonical = [r for r in rows if r["kind"] == "canonical"]
    evasion = [r for r in rows if r["kind"] == "evasion"]
    benign = [r for r in rows if r["kind"] == "benign"]
    attacks = canonical + evasion
    return {
        "suite": "checker_asr",
        "fixture": "backend/tests/fixtures/injection_cases.json",
        "definition": "ASR = fraction of attack cases whose injected plan was NOT flagged by check_plan",
        "n_total": len(rows),
        "n_canonical": len(canonical),
        "n_evasion": len(evasion),
        "n_benign": len(benign),
        "canonical_scan_detection_rate": _rate(canonical, "scan_detected"),
        "canonical_plan_flag_rate": _rate(canonical, "plan_flagged"),
        "canonical_asr": round(1 - (_rate(canonical, "plan_flagged") or 0), 4),
        "evasion_scan_detection_rate": _rate(evasion, "scan_detected"),
        "evasion_plan_flag_rate": _rate(evasion, "plan_flagged"),
        "evasion_asr": round(1 - (_rate(evasion, "plan_flagged") or 0), 4),
        "overall_plan_flag_rate": _rate(attacks, "plan_flagged"),
        "overall_asr": round(1 - (_rate(attacks, "plan_flagged") or 0), 4),
        "benign_false_positive_rate": _rate(benign, "plan_flagged"),
        "by_family": by_family,
        "cases": rows,
    }


# ---------------------------------------------------------------------------
# Privacy leak rate
# ---------------------------------------------------------------------------

def _pii_observation(case: dict) -> BrowserObservation:
    if case["context"] == "element_value":
        el = InteractiveElement.model_validate({
            "targetId": "e1", "role": "textbox", "name": case.get("element_name", "Field"),
            "tag": "input", "inputType": "text", "value": case["token"],
            "visible": True, "enabled": True,
        })
        return BrowserObservation.model_validate({
            "tabId": 1, "url": "http://test.local/page", "title": "Test page",
            "interactive": [el],
            "scroll": {"y": 0, "viewport": {"w": 900, "h": 600}}, "dpr": 1,
            "pageText": "Ordinary page content about products and prices.",
        })
    text = (
        "Welcome to the demo shop. Some context here. "
        f"{case['token']} More ordinary page content about products and prices."
    )
    return BrowserObservation.model_validate({
        "tabId": 1, "url": "http://test.local/page", "title": "Test page",
        "interactive": [],
        "scroll": {"y": 0, "viewport": {"w": 900, "h": 600}}, "dpr": 1,
        "pageText": text,
    })


def run_privacy_leak() -> dict:
    fixture = _load_fixture("pii_cases.json")
    rows = []
    for case in fixture["cases"]:
        obs = _pii_observation(case)
        payload, _ = classify_and_redact(obs, instruction="find headphones")
        prompt = build_prompt("find headphones", payload, [])
        token = case["token"]
        # A URL token is a leak only if the sensitive value itself survives;
        # the host/path may legitimately remain after redact_url.
        leaked = token in prompt
        rows.append({
            "id": case["id"],
            "type": case["type"],
            "context": case["context"],
            "leaked": leaked,
        })

    by_type: dict[str, dict] = {}
    for pii_type in sorted({r["type"] for r in rows}):
        sub = [r for r in rows if r["type"] == pii_type]
        by_type[pii_type] = {
            "n": len(sub),
            "leaked": sum(1 for r in sub if r["leaked"]),
            "leak_rate": round(sum(1 for r in sub if r["leaked"]) / len(sub), 4),
        }
    by_context: dict[str, dict] = {}
    for ctx in sorted({r["context"] for r in rows}):
        sub = [r for r in rows if r["context"] == ctx]
        by_context[ctx] = {
            "n": len(sub),
            "leaked": sum(1 for r in sub if r["leaked"]),
            "leak_rate": round(sum(1 for r in sub if r["leaked"]) / len(sub), 4),
        }
    total = len(rows)
    leaked_n = sum(1 for r in rows if r["leaked"])
    return {
        "suite": "privacy_leak",
        "fixture": "backend/tests/fixtures/pii_cases.json",
        "definition": "leak = the exact synthetic PII token string appears in the planner prompt after classify_and_redact + build_prompt",
        "n_total": total,
        "leaked": leaked_n,
        "leak_rate": round(leaked_n / total, 4) if total else None,
        "by_type": by_type,
        "by_context": by_context,
        "cases": rows,
    }


# ---------------------------------------------------------------------------
# Mock benchmark A-H (with trajectories)
# ---------------------------------------------------------------------------

def run_mock_benchmark(trajectory_path: Path) -> dict:
    from scripts.benchmark_eval import run_benchmark

    records: list[dict] = []
    report = run_benchmark(trajectory_out=trajectory_path)
    # run_benchmark writes the JSONL itself; read it back for the summary.
    if trajectory_path.exists():
        records = [json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "suite": "mock_benchmark",
        "mode": "deterministic_fixture",
        "note": "Mock fixtures only, explicitly NOT live autonomy metrics.",
        "passed": report["passed"],
        "total": report["total"],
        "average_fixture_duration_ms": report["average_fixture_duration_ms"],
        "scenarios": records,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["all", "checker", "privacy", "mock"], default="all")
    parser.add_argument("--out-dir", type=Path, default=REPO_DIR / "evals")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--provider", default="mock",
                        help="mock only. Any other value fails: no live provider is wired in this harness.")
    args = parser.parse_args(argv)

    if args.provider != "mock":
        print(f"provider {args.provider!r} is not runnable here: a live run needs one of "
              f"{', '.join(REAL_PROVIDER_ENV)} set plus a connected Chrome extension. "
              "See docs/EVALS.md.", file=sys.stderr)
        return 2

    out_dir: Path = args.out_dir
    results_dir = out_dir / "results"
    traj_dir = out_dir / "trajectories"
    results_dir.mkdir(parents=True, exist_ok=True)
    traj_dir.mkdir(parents=True, exist_ok=True)

    summary: dict = {
        "generated": args.date,
        "provider": "mock",
        "command": " ".join(sys.argv),
        "suites": {},
    }
    started = time.perf_counter()

    if args.suite in ("all", "checker"):
        print("== checker ASR ==")
        checker = run_checker_asr()
        (results_dir / f"checker_asr_{args.date}.json").write_text(
            json.dumps(checker, indent=2), encoding="utf-8")
        summary["suites"]["checker_asr"] = {
            "n_total": checker["n_total"],
            "canonical_asr": checker["canonical_asr"],
            "evasion_asr": checker["evasion_asr"],
            "overall_asr": checker["overall_asr"],
            "benign_false_positive_rate": checker["benign_false_positive_rate"],
        }
        print(f"  overall ASR {checker['overall_asr']} "
              f"(canonical {checker['canonical_asr']}, evasion {checker['evasion_asr']})")

    if args.suite in ("all", "privacy"):
        print("== privacy leak rate ==")
        leak = run_privacy_leak()
        (results_dir / f"privacy_leak_{args.date}.json").write_text(
            json.dumps(leak, indent=2), encoding="utf-8")
        summary["suites"]["privacy_leak"] = {
            "n_total": leak["n_total"],
            "leaked": leak["leaked"],
            "leak_rate": leak["leak_rate"],
        }
        print(f"  leak rate {leak['leak_rate']} ({leak['leaked']}/{leak['n_total']} tokens)")

    if args.suite in ("all", "mock"):
        print("== mock benchmark A-H ==")
        traj_path = traj_dir / f"mock_benchmark_{args.date}.jsonl"
        mock = run_mock_benchmark(traj_path)
        (results_dir / f"mock_benchmark_{args.date}.json").write_text(
            json.dumps(mock, indent=2), encoding="utf-8")
        summary["suites"]["mock_benchmark"] = {
            "passed": mock["passed"],
            "total": mock["total"],
            "average_fixture_duration_ms": mock["average_fixture_duration_ms"],
        }
        print(f"  {mock['passed']}/{mock['total']} fixtures passed")

    summary["elapsed_s"] = round(time.perf_counter() - started, 2)
    (results_dir / f"summary_{args.date}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote results to {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
