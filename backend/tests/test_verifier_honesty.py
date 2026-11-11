"""Verifier honesty and execution-history regression tests.

Covers two Phase-1 correctness fixes:

1. Every early-return path of the agent loop carries the execution history,
   so downstream verification and checkpoint restore always have evidence.
2. The completion verifier never launders an execution failure into a
   successful completion.
"""
from __future__ import annotations

import ast
import os
import re

from app.completion_verifier import (
    is_execution_failure,
    verify_task_completion,
)


# ---------------------------------------------------------------------------
# Part 1: every return statement in agentLoop._run_loop carries "history"
# ---------------------------------------------------------------------------

def _run_loop_returns_with_history() -> list[tuple[int, str]]:
    """Parse agentLoop.py and report _run_loop returns lacking a history key."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app",
        "agentLoop.py",
    )
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)

    loop_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_loop":
            loop_fn = node
            break
    assert loop_fn is not None, "_run_loop not found in agentLoop.py"

    bad: list[tuple[int, str]] = []
    # Only returns that lexically belong to _run_loop itself — nested helper
    # functions (progress, metrics) defined inside it legitimately return
    # non-dict values and are excluded.
    loop_start = loop_fn.lineno
    loop_end = loop_fn.end_lineno

    class _ReturnVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func_depth = 0

        def _visit_children(self, node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    self.func_depth += 1
                    self.visit(child)
                    self.func_depth -= 1
                else:
                    self.visit(child)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_children(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_children(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self._visit_children(node)

        def visit_Return(self, node: ast.Return) -> None:
            if self.func_depth == 0:
                if not isinstance(node.value, ast.Dict):
                    bad.append((node.lineno, "non-dict return"))
                    return
                keys = []
                for k in node.value.keys:
                    if isinstance(k, ast.Constant):
                        keys.append(k.value)
                if "history" not in keys:
                    bad.append((node.lineno, source.splitlines()[node.lineno - 1].strip()))

    visitor = _ReturnVisitor()
    visitor.visit(loop_fn)
    assert loop_start > 0 and loop_end > loop_start  # sanity: function located
    return bad


def test_every_run_loop_return_path_includes_history():
    missing = _run_loop_returns_with_history()
    assert not missing, (
        "agentLoop._run_loop return paths without 'history' key: "
        + ", ".join(f"line {ln}: {txt}" for ln, txt in missing)
    )


# ---------------------------------------------------------------------------
# Part 2: verifier never upgrades failure to completed
# ---------------------------------------------------------------------------

def test_execution_failure_detector():
    assert is_execution_failure("Planner unavailable: quota exhausted")
    assert is_execution_failure("Planner timed out after 20s")
    assert is_execution_failure("Loop detected: action 'click' repeated")
    assert is_execution_failure("non-recoverable error: CDP connection lost")
    assert is_execution_failure("privacy violation — payload not sent")
    assert is_execution_failure("Error: something went wrong")
    assert is_execution_failure("SESSION_INIT_FAILED: no tab")
    assert is_execution_failure("TASK_TIMEOUT: step budget exhausted")
    assert not is_execution_failure("Found the product at ₹24,999 on Amazon")
    assert not is_execution_failure("")
    assert not is_execution_failure("Task completed with all three laptops compared.")


def test_loop_detect_is_failed_not_completed():
    res = verify_task_completion(
        instruction="find a laptop on amazon",
        candidate_state="failed",
        message="Loop detected: action 'click' repeated on identical page state with zero effect.",
    )
    assert res.state == "failed"
    assert res.is_verified is False


def test_non_recoverable_error_is_failed_not_completed():
    res = verify_task_completion(
        instruction="open google",
        candidate_state="failed",
        message="non-recoverable error: SESSION_INIT_FAILED",
    )
    assert res.state == "failed"


def test_planner_failure_keeps_original_message():
    original = "Planner timed out: model provider did not respond within 20s deadline (step 3 state preserved)."
    res = verify_task_completion(
        instruction="compare prices",
        candidate_state="failed",
        message=original,
    )
    assert res.state == "failed"
    # The underlying error must survive, not be masked by a research-incomplete rewrite
    assert "Planner timed out" in res.message


def test_conversational_failure_stays_failed():
    res = verify_task_completion(
        instruction="explain transformers",
        candidate_state="failed",
        message="Task failed: no response path available.",
    )
    assert res.state == "failed"
    assert res.is_verified is False


def test_conversation_candidate_state_preserved():
    res = verify_task_completion(
        instruction="hi there",
        candidate_state="failed",
        message="greeting queued",
    )
    # A failed candidate must never be rewritten to completed by the fallback.
    assert res.state == "failed"


def test_genuine_conversational_still_completes():
    res = verify_task_completion(
        instruction="explain what a transformer is",
        candidate_state="completed",
        message="A transformer is a neural architecture based on attention.",
    )
    assert res.state == "completed"
    assert res.is_verified is True


def test_budget_exceeded_marker_is_failed():
    res = verify_task_completion(
        instruction="find a laptop",
        candidate_state="completed",
        message="budget_exceeded before any result was found",
    )
    assert res.state == "failed"
    assert res.is_verified is False
