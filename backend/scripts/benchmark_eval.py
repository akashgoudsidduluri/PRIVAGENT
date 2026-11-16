"""Deterministic fixture smoke tests A–H. These are not live autonomy metrics."""
import json
import sys
import time
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

from tests.test_scenarios_a_to_h import (
    test_scenario_a_google_search,
    test_scenario_b_search_scroll_and_compare,
    test_scenario_c_zoom_and_visual_interaction,
    test_scenario_d_multi_tab_workflow,
    test_scenario_e_form_handling_without_submit,
    test_scenario_f_dynamic_page_recovery,
    test_scenario_g_privacy_and_prompt_injection,
    test_scenario_h_consequential_action_confirmation,
)


def run_benchmark(trajectory_out: Path | None = None) -> dict:
    """Run the eight fixture scenarios.

    When ``trajectory_out`` is given, one JSON line per scenario is
    appended (name, passed, duration_ms, error) — the trajectory record
    for the mock harness. Fixture data is synthetic; nothing sensitive
    is written. Returns the summary report dict.
    """
    scenarios = [
        ("Google search fixture", test_scenario_a_google_search),
        ("Search and comparison fixture", test_scenario_b_search_scroll_and_compare),
        ("Zoom fixture", test_scenario_c_zoom_and_visual_interaction),
        ("Multi-tab fixture", test_scenario_d_multi_tab_workflow),
        ("Form fixture", test_scenario_e_form_handling_without_submit),
        ("Dynamic recovery fixture", test_scenario_f_dynamic_page_recovery),
        ("Privacy fixture", test_scenario_g_privacy_and_prompt_injection),
        ("Approval fixture", test_scenario_h_consequential_action_confirmation),
    ]
    passed = 0
    durations = []
    records: list[dict] = []
    for name, fn in scenarios:
        started = time.perf_counter()
        error: str | None = None
        try:
            fn()
            passed += 1
            print(f"  [PASS] {name}")
        except Exception as exc:
            error = str(exc)
            print(f"  [FAIL] {name}: {exc}")
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        durations.append(duration_ms)
        records.append({
            "scenario": name,
            "passed": error is None,
            "duration_ms": duration_ms,
            "error": error,
            "mode": "deterministic_fixture",
        })
    if trajectory_out is not None:
        trajectory_out.parent.mkdir(parents=True, exist_ok=True)
        # Task 10: under zero retention, trajectory records are scrubbed
        # summaries only — an error string must never carry page text or
        # a credential into a file on disk.
        try:
            from app.zero_retention import is_enabled as _zr_enabled
            from app.zero_retention import redact_trajectory_record as _zr_redact
        except Exception:
            _zr_enabled = lambda: False  # noqa: E731
            _zr_redact = lambda r: r  # noqa: E731
        with trajectory_out.open("w", encoding="utf-8") as fh:
            for record in records:
                if _zr_enabled():
                    record = _zr_redact(record)
                fh.write(json.dumps(record) + "\n")
    report = {"mode": "deterministic_fixture", "passed": passed, "total": len(scenarios),
              "average_fixture_duration_ms": round(sum(durations) / len(durations), 2)}
    print("Fixture smoke tests:", report)
    print("No live task-completion, action-accuracy, recovery or privacy-leak rate is inferred.")
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deterministic fixture smoke tests A-H (mock only).")
    parser.add_argument("--trajectory-out", type=Path, default=None,
                        help="Write one JSON line per scenario to this file.")
    cli_args = parser.parse_args()
    report = run_benchmark(trajectory_out=cli_args.trajectory_out)
    if report["passed"] < report["total"]:
        sys.exit(1)
    sys.exit(0)
