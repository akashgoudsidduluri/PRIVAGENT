"""Zero-retention mode (Task 10).

When ``PRIVAGENT_ZERO_RETENTION=true`` the loop keeps nothing that
could outlive the current step:

* **No checkpoints.** :func:`checkpoints_allowed` is False, so the
  loop skips ``save_task_checkpoint`` entirely — a zero-retention
  task cannot be resumed after a restart, by design.
* **No screenshots to the planner.** The observation's screenshot is
  dropped before planning, so a rendered page (which may show a
  password, card, or OTP the DOM redactor never saw) never reaches a
  provider.
* **No planner payloads in logs.** Provider telemetry logging becomes
  a no-op (see ``providers/limits.log_provider_call``).
* **Pruned in-memory history.** After each step the history is cut to
  the last ``ZERO_RETENTION_HISTORY_KEEP`` entries (default 3) and
  each kept entry is stripped to the fields loop detection needs
  (action type, result, status, scrubbed error). Evidence and
  narratives — which can quote page text — are dropped.
* **No agent chat persistence.** ``run_task``'s post-loop
  finalisation used to persist the agent summary (which quotes the
  last evidence) and the extracted mission facts (``last_result`` =
  summary[:240]) into the chat SQLite tables even with zero
  retention on — only the checkpoint path was gated. Agent chat
  messages and derived mission facts are now skipped too (see
  :func:`chat_persistence_allowed`); the operator still sees the
  message live through the record/broadcast.
* **Redacted trajectory records.** The eval harness writes only
  scrubbed summaries when this mode is on (see
  :func:`redact_trajectory_record`).

What zero retention does NOT cover: the operator's own long-term
memory store (Task 9) is a separate, explicit store and is
unaffected — existing facts/skills are kept and still recalled, and
a fact written directly (API/UI) while the mode is on still lands.
But a zero-retention task produces NO memory write-back: proposals
are stored in the task checkpoint, and there is no checkpoint, so
nothing from such a task is learned. The operator's own user
message (their instruction, persisted when the task is created) and
instruction-derived research metadata (topic/format/type) also
still persist — they are the operator's own words, not page text.
The browser itself keeps its own state (history, cache)
in the agent profile; and the in-memory history of the *running*
task is visible to the operator through the live API until the
process ends. See ``docs/PRIVACY.md``.
"""
from __future__ import annotations

from typing import Any


def is_enabled() -> bool:
    """True when zero-retention mode is on (read fresh from config)."""
    try:
        from . import config

        return bool(config.ZERO_RETENTION)
    except Exception:
        return False


def checkpoints_allowed() -> bool:
    """Checkpoints are skipped entirely under zero retention."""
    return not is_enabled()


def chat_persistence_allowed() -> bool:
    """Agent chat messages / derived mission facts may be persisted.

    REVIEW (Task 10 audit): only the checkpoint path was gated, so
    ``run_task`` still wrote the agent summary and extracted facts
    (both can quote page text) into the chat tables. They are a
    second persistence surface and are gated the same way. The
    operator's own user message is NOT covered by this — it is the
    operator's own instruction, persisted by the caller directly.
    """
    return not is_enabled()


def history_keep() -> int:
    try:
        from . import config

        return max(1, int(config.ZERO_RETENTION_HISTORY_KEEP))
    except Exception:
        return 3


_MINIMAL_ENTRY_KEYS = ("result_ok", "status", "error")


def prune_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prune a task history in place to the minimum the loop needs.

    Keeps the last N entries (N = ``ZERO_RETENTION_HISTORY_KEEP``)
    and strips each kept entry to ``action.type``, ``result_ok``,
    ``status`` and the (already redacted) ``error`` — the fields
    ``_same_failure`` loop detection reads. ``evidence`` and
    ``narrative`` are dropped: both can quote page text. Returns the
    same list object, mutated.
    """
    keep = history_keep()
    if len(history) > keep:
        del history[:-keep]
    for idx, entry in enumerate(history):
        action = entry.get("action") or {}
        minimal: dict[str, Any] = {
            "action": {"type": action.get("type")} if isinstance(action, dict) else {"type": None},
        }
        for key in _MINIMAL_ENTRY_KEYS:
            if key in entry:
                minimal[key] = entry[key]
        history[idx] = minimal
    return history


def strip_screenshot(payload: Any, raw_obs: Any) -> None:
    """Drop a screenshot from the redacted payload and the raw observation.

    Best effort and silent: a screenshot must never reach the planner
    in zero-retention mode, even if the observation shape is unusual.
    """
    try:
        payload.screenshot = None
    except Exception:
        pass
    try:
        if isinstance(raw_obs, dict):
            raw_obs["screenshot"] = None
        else:
            raw_obs.screenshot = None
    except Exception:
        pass


def redact_trajectory_record(record: dict[str, Any]) -> dict[str, Any]:
    """Scrub one trajectory record for zero-retention output.

    Trajectory records carry only scenario metadata (name, passed,
    duration, error). The error string is scrubbed of credential-like
    values with the same scrub as the audit log; any unexpected
    page-text-bearing keys are dropped outright. Returns a new dict.
    """
    kept: dict[str, Any] = {}
    for key in ("scenario", "passed", "duration_ms", "mode"):
        if key in record:
            kept[key] = record[key]
    if record.get("error") is not None:
        # Same scrub as the audit log: credential-pattern values are
        # removed, the message shape is kept. Arbitrary page text can
        # never reach this field in the real harness (errors are
        # fixture assertion messages), and page-text-bearing keys are
        # dropped outright below — the scrub is defence in depth.
        try:
            from .audit import scrub_summary as _audit_scrub

            err = _audit_scrub(str(record["error"]))
        except Exception:
            err = str(record["error"])
        kept["error"] = err[:500]
    return kept
