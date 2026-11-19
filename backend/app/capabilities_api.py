"""User-facing capability routing with a one-use gate for native mutations."""
from __future__ import annotations

import hashlib
import inspect
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .capabilities import CapabilityRegistry
from .hub import hub
from .mcp.policy import ApprovalPolicy
from .mcp.sanitizer import sanitize_tool_output

router = APIRouter(prefix="/api/capabilities")
_policy = ApprovalPolicy()
_pending: dict[str, tuple[float, str, str, dict, str]] = {}


class CallInput(BaseModel):
    capability: str
    operation: str
    args: dict[str, Any] = Field(default_factory=dict)
    gate_task_id: str | None = Field(default=None, alias="gateTaskId")
    model_config = {"populate_by_name": True}


def _registry(user_id: str | None = None) -> CapabilityRegistry:
    """Build the registry for one caller (a fresh registry per call,
    so a user id is cheap to thread through)."""
    registry = CapabilityRegistry(browser_available=bool(hub.extension_sockets), user_id=user_id)
    registry._policy = _policy
    return registry


def _binding(body: CallInput, user_id: str | None = None) -> str:
    """The gate binding includes the caller, so a gate prepared by one
    user cannot be executed by another even if the gate task id leaks."""
    return hashlib.sha256(json.dumps([user_id, body.capability, body.operation, body.args],
                                     sort_keys=True, default=str).encode()).hexdigest()


def _caller_user_id(request: Request) -> str | None:
    return getattr(request.state, "user_id", None)


def _validate(registry: CapabilityRegistry, body: CallInput):
    try:
        route = registry.resolve(body.capability, body.operation)
    except ValueError:
        raise HTTPException(404, "unsupported capability operation") from None
    if route.method != "native":
        raise HTTPException(409, "no native connector available")
    adapter = next(a for a in registry.adapters[body.capability] if type(a).__name__ == route.adapter)
    try:
        inspect.signature(getattr(adapter, body.operation)).bind(**body.args)
    except (AttributeError, TypeError):
        raise HTTPException(422, "invalid connector arguments") from None
    return route


@router.get("")
def routes(request: Request):
    registry = _registry(user_id=_caller_user_id(request))
    return [{**registry.resolve(capability, operation).__dict__, "mutating": mutating}
            for capability, operations in registry._OPERATIONS.items()
            for operation, mutating in operations.items()]


@router.post("/prepare")
async def prepare(request: Request, body: CallInput):
    user_id = _caller_user_id(request)
    registry = _registry(user_id=user_id)
    _validate(registry, body)
    if not registry._OPERATIONS[body.capability][body.operation]:
        raise HTTPException(409, "read operation does not require approval")
    task = hub.new_task(f"Approve {body.capability}.{body.operation}", user_id=user_id)
    task.state = "awaiting_confirmation"
    task.pending_confirmation = {"message": f"Approve {body.capability}.{body.operation} with: "
                                 + sanitize_tool_output(body.args, 1000)["content"],
                                 "action": {"type": "native_connector", "capability": body.capability,
                                            "operation": body.operation}}
    _pending[task.task_id] = (time.monotonic() + 120, body.capability, body.operation, body.args, _binding(body, user_id), user_id)
    await hub.broadcast_progress(task)
    return {"gateTaskId": task.task_id, "message": task.pending_confirmation["message"]}


@router.post("/execute")
async def execute(request: Request, body: CallInput):
    user_id = _caller_user_id(request)
    registry = _registry(user_id=user_id)
    _validate(registry, body)
    mutating = registry._OPERATIONS[body.capability][body.operation]
    task = None
    token = None
    if mutating:
        pending = _pending.pop(body.gate_task_id or "", None)
        task = hub.tasks.get(body.gate_task_id or "")
        # The gate belongs to the user who prepared it: another
        # session user presenting the gate task id gets a fresh-gate
        # error, not the execution. The operator may execute any gate.
        gate_owner = pending[5] if pending and len(pending) > 5 else None
        # Gate-owner mismatch is a not-found, not a forbidden: the
        # caller must not learn another user's gate exists.
        if pending is not None and user_id is not None and gate_owner != user_id:
            raise HTTPException(404, "gate not found")
        if (not pending or not task or pending[0] < time.monotonic() or
            pending[1:3] != (body.capability, body.operation) or
            pending[4] != _binding(body, user_id) or task.state != "awaiting_confirmation" or
            task.confirm_decision is not True or
            (user_id is not None and gate_owner != user_id)):
            raise HTTPException(403, "fresh approved connector gate required")
        token = registry.approve_native(body.capability, body.operation, body.args, task.task_id)
        task.state = "running"
        await hub.broadcast_progress(task)
    try:
        result = await run_in_threadpool(registry.execute_native, body.capability, body.operation, body.args,
                                         approval_token=token, task_id=body.gate_task_id or "")
    except Exception:
        if task:
            task.state = "failed"
            task.message = "Connector call failed."
            await hub.broadcast_progress(task)
        raise HTTPException(502, "connector call failed") from None
    if task:
        task.state = "failed"
        task.message = "Connector returned a response; confirm the external effect at the service."
        await hub.broadcast_progress(task)
        return {**result, "effect_verified": False}
    return result
