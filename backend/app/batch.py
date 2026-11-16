"""Batch selection for the agent loop (Task 11).

A plan is a list of steps, but the loop used to execute only
``steps[0]`` and then pay for a whole new planner round trip — a new
observation, a new prompt, a new model call — for ``steps[1]``. When
the plan's next steps are still valid against the observation the
plan was built from, executing them without re-planning is pure win:
fewer planner calls, fewer observations, same safety.

Safety rules (do not relax):

* A batch is a run of consecutive non-submitting ``type`` steps —
  typing into a field does not remove elements, navigate, or submit,
  so the refs the plan quoted stay valid — optionally followed by
  exactly ONE terminating step (a click, a submitting type, a
  navigation, ...). The terminating step ends the batch: after it
  the page may have changed, so the loop re-observes and re-plans.
  "Does not change the page" is not taken on trust: the loop only
  continues a batch when the post-action observation's DOM
  fingerprint still matches the observation the plan was built from
  (``agentLoop`` compares them at the batch gate). A type step that
  opens an autocomplete dropdown, shows a validation message, or
  otherwise mutates the DOM ends the batch and the loop re-plans on
  the fresh tree. The fingerprint is coarse (URL + title + element
  count + first 12 target ids — a name/role change past the first 12
  does not move it), so the extension's live name/role agreement
  check on every executed step remains the backstop.
* A step that is not the first step of the plan is batched ONLY if
  it is ref-grounded: it quotes an ``elementRef`` present in the
  observation the plan was built from, or a stable author
  ``targetId``. A name-only step is never batched — its live-DOM
  name match could shift after an earlier batched step typed into
  the page.
* A step that addresses an element (click / type / select_option /
  set_date / upload_file) without a ref or a targetId is never a
  batched step.
* Dependent steps cannot be batched by construction: an element
  that only appears after a prior click is not in the observation
  the plan was built from, so its ref is unknown and the plan
  validator (``planner._validate_plan``) rejects the step before
  the loop ever sees it.
* Batching never merges confirmation decisions: every step keeps
  its own confirmation / takeover / allow-list / checker gates in
  the loop. A risky step inside a batch still pauses for its own
  confirmation before it executes. Batching elides only the
  planner call between steps — never a gate.

The loop computes the batch once per plan (``batch_prefix``) and
then executes one step per iteration from it, without re-planning,
until the batch is exhausted or a step fails / changes the page.
"""
from __future__ import annotations

from typing import Any, Optional

# Steps that address a page element. Only these need ref grounding.
ELEMENT_ACTIONS = {"click", "type", "select_option", "set_date", "upload_file"}

# A batch may contain at most this many steps. Plans are already
# capped at MAX_STEPS_PER_PLAN (3) by the planner, so this is a
# backstop, not the binding limit.
MAX_BATCH_STEPS = 3


def _action_of(step: Any) -> dict[str, Any]:
    if isinstance(step, dict):
        action = step.get("action")
        return action if isinstance(action, dict) else {}
    action = getattr(step, "action", None)
    if isinstance(action, dict):
        return action
    # Pydantic models (PlannedStep / Action) expose attributes.
    if action is not None:
        return {
            "type": getattr(action, "type", None),
            "target": getattr(action, "target", None),
            "params": getattr(action, "params", None),
        }
    return {}


def _target_of(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target")
    if isinstance(target, dict):
        return target
    if target is None:
        return {}
    # Pydantic target model.
    return {
        "targetId": getattr(target, "target_id", None),
        "target_id": getattr(target, "target_id", None),
        "name": getattr(target, "name", None),
        "elementRef": getattr(target, "element_ref", None),
        "element_ref": getattr(target, "element_ref", None),
    }


def _params_of(action: dict[str, Any]) -> dict[str, Any]:
    params = action.get("params")
    if isinstance(params, dict):
        return params
    if params is None:
        return {}
    return {
        "submit": getattr(params, "submit", None),
        "text": getattr(params, "text", None),
        "url": getattr(params, "url", None),
    }


def is_non_submitting_type(step: Any) -> bool:
    """True for a ``type`` step that does not submit the form."""
    action = _action_of(step)
    if action.get("type") != "type":
        return False
    return not bool(_params_of(action).get("submit"))


def is_ref_grounded(step: Any, valid_refs: Optional[set[str]]) -> bool:
    """True when the step's target is pinned to the plan observation.

    A quoted elementRef must be one the observation actually
    contains; a stable author targetId also pins the element (the
    Moodle fix treats it as a disambiguator). A bare name does not.
    """
    action = _action_of(step)
    target = _target_of(action)
    ref = target.get("elementRef") or target.get("element_ref")
    if ref:
        if valid_refs is None:
            # No observation to check against (unit tests driving
            # validation directly): a quoted ref is grounded enough.
            return True
        return str(ref) in valid_refs
    if target.get("targetId") or target.get("target_id"):
        return True
    return False


def batch_prefix(
    steps: list[Any],
    valid_refs: Optional[set[str]],
    max_batch: int = MAX_BATCH_STEPS,
) -> list[Any]:
    """The executable prefix of a plan: the steps the loop may run
    without re-planning.

    Always includes ``steps[0]`` (a one-step batch behaves exactly
    like the old loop). Extends only over a run of ref-grounded
    non-submitting ``type`` steps, plus at most one terminating step
    after them. Anything else stops the batch.
    """
    if not steps:
        return []
    batch = [steps[0]]
    if not is_non_submitting_type(steps[0]):
        return batch
    for step in steps[1:max_batch]:
        action = _action_of(step)
        if is_non_submitting_type(step):
            if not is_ref_grounded(step, valid_refs):
                break
            batch.append(step)
            continue
        # Terminating step: allowed as the final batch element, but
        # an element-addressing one must still be ref-grounded.
        if action.get("type") in ELEMENT_ACTIONS and not is_ref_grounded(step, valid_refs):
            break
        batch.append(step)
        break
    return batch
