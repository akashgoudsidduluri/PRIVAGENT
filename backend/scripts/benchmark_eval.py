"""Deterministic fixture smoke tests A–H. These are not live autonomy metrics."""
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


def run_benchmark() -> dict:
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
    for name, fn in scenarios:
        started = time.perf_counter()
        try:
            fn()
            passed += 1
            print(f"  [PASS] {name}")
        except Exception as exc:
            print(f"  [FAIL] {name}: {exc}")
        durations.append((time.perf_counter() - started) * 1000)
    report = {"mode": "deterministic_fixture", "passed": passed, "total": len(scenarios),
              "average_fixture_duration_ms": round(sum(durations) / len(durations), 2)}
    print("Fixture smoke tests:", report)
    print("No live task-completion, action-accuracy, recovery or privacy-leak rate is inferred.")
    return report


if __name__ == "__main__":
    report = run_benchmark()
    if report["passed"] < report["total"]:
        sys.exit(1)
    sys.exit(0)
