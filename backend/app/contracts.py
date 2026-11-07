"""Contract mirror of extension/src/contracts.ts.

Actions cross a language boundary (TS extension ↔ Python backend) — the
literal lists below must stay byte-identical to the TS source; the parity
test reads the TS file and asserts equality. If you change one side, change
both and update extension/src/contracts.ts accordingly.
"""
from __future__ import annotations
from enum import Enum
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

ACTION_TYPES = [
    "navigate", "click", "type", "press", "scroll", "zoom",
    "wait", "observe", "select_tab", "new_tab", "close_tab",
    "hover", "select_date", "slider_adjust",
]
RISK_CLASSES = ["read", "write", "sensitive"]
TASK_STATES = [
    "planning", "awaiting_confirmation", "running", "awaiting_input",
    "completed", "failed", "cancelled",
]
ERROR_CODES = [
    "STALE_TARGET", "NO_EFFECT", "TIMEOUT", "NAVIGATION_FAILED",
    "PRIVACY_VIOLATION", "PLAN_INVALID", "BUDGET_EXCEEDED", "CANCELLED",
    "EXTENSION_DISCONNECTED", "UNKNOWN",
]

RiskClass = Literal["read", "write", "sensitive"]
TaskState = Literal[*TASK_STATES]  # type: ignore[valid-type]
AgentErrorCode = Literal[*ERROR_CODES]  # type: ignore[valid-type]

ElementTarget = dict[str, Any]


class ActionParams(BaseModel):
    url: Optional[str] = None
    direction: Optional[str] = None
    text: Optional[str] = None
    clear: Optional[bool] = None
    submit: Optional[bool] = None
    key: Optional[str] = None
    container_id: Optional[str] = Field(default=None, alias="containerId")
    tab_id: Optional[int] = Field(default=None, alias="tabId")
    factor: Optional[float] = None
    date: Optional[str] = None
    percent: Optional[float] = None

    model_config = {"populate_by_name": True}


class BrowserAction(BaseModel):
    """Loose over-union of the TS discriminated union. The planner validates
    the tight union per-action-type before execution; this model carries
    payloads across the WebSocket."""

    type: str
    risk: RiskClass
    verify: str
    timeout_ms: int = Field(alias="timeoutMs")
    target: Optional[ElementTarget] = None
    params: Optional[ActionParams] = None

    model_config = {"populate_by_name": True}


class InteractiveElement(BaseModel):
    target_id: str = Field(alias="targetId")
    role: str
    name: str
    tag: str
    input_type: Optional[str] = Field(default=None, alias="inputType")
    value: Optional[str] = None
    visible: bool
    enabled: bool
    bounds: Optional[dict[str, float]] = None

    model_config = {"populate_by_name": True}


class BrowserObservation(BaseModel):
    tab_id: int = Field(alias="tabId")
    url: str
    title: str
    interactive: list[InteractiveElement]
    scroll: dict[str, Any]
    dpr: float = 1.0
    screenshot: Optional[str] = None
    tabs: Optional[list[dict[str, Any]]] = None
    article_content: Optional[str] = Field(default=None, alias="articleContent")
    table_rows: Optional[list[str]] = Field(default=None, alias="tableRows")
    overlay_text: Optional[str] = Field(default=None, alias="overlayText")

    model_config = {"populate_by_name": True}


class ActionResult(BaseModel):
    ok: bool
    target_id: Optional[str] = Field(default=None, alias="targetId")
    observation: Optional[BrowserObservation] = None
    error: Optional[dict[str, Any]] = None
    evidence: Optional[str] = None

    model_config = {"populate_by_name": True}


class AgentError(BaseModel):
    code: AgentErrorCode
    message: str
    recoverable: bool


class PlannedStep(BaseModel):
    action: BrowserAction
    why: str


class TaskPlan(BaseModel):
    goal: str
    steps: list[PlannedStep]
    answer: Optional[str] = None
    notes: Optional[str] = None
    # Set by the planner guard when the run is honestly reported as incomplete.
    # The loop turns this into a failed task instead of "Mission Completed".
    blocked: bool = False


class UserTask(BaseModel):
    id: str
    instruction: str
    status: TaskState = "planning"


class TaskProgress(BaseModel):
    task_id: str = Field(alias="taskId")
    task_state: TaskState = Field(alias="taskState")
    steps_done: int = Field(alias="stepsDone")
    message: str

    model_config = {"populate_by_name": True}


class PrivacyDecision(BaseModel):
    send: bool
    reason: str
    violations: list[str] = []


class PrivacyViolation(Exception):
    """Fail-closed marker — raised by the privacy layer, never caught into a partial send."""
