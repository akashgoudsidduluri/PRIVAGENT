/**
 * Shared action/observation contracts — the TS source of truth.
 * backend/app/contracts.py mirrors these; a parity test pins drift.
 */

export const ACTION_TYPES = [
  "navigate",
  "click",
  "type",
  "press",
  "scroll",
  "zoom",
  "wait",
  "observe",
  "select_tab",
  "new_tab",
  "close_tab",
] as const;

export type ActionType = (typeof ACTION_TYPES)[number];

export const RISK_CLASSES = ["read", "write", "sensitive"] as const;
export type RiskClass = (typeof RISK_CLASSES)[number];

const RISK_ORDER: Record<RiskClass, number> = { read: 0, write: 1, sensitive: 2 };

/** Minimum acceptable risk classification per action type. */
export const TYPE_ACTION_RISK_FLOOR: Record<ActionType, RiskClass> = {
  navigate: "write",
  click: "write",
  type: "write",
  press: "write",
  scroll: "read",
  zoom: "read",
  wait: "read",
  observe: "read",
  select_tab: "read",
  new_tab: "write",
  close_tab: "write",
};

export interface ElementTarget {
  targetId: string;
  /** Semantic anchor — accessible name the observer reported. */
  name?: string;
  /** ARIA role hint (planner may pass it; used only as a tiebreaker). */
  role?: string;
  /** Explicit semantic marker. Only set when the action is intentionally a search. */
  search?: boolean;
  /** Fallback CSS selector from the observer registry. */
  cssSelector?: string;
  /** Screenshot-grounded coordinates in CSS pixels (last resort). */
  coords?: { x: number; y: number };
}

interface ActionBase {
  risk: RiskClass;
  /** Verification criteria — checked against a fresh observation. */
  verify: string;
  timeoutMs: number;
  sessionId?: string;
  tabId?: number;
}

export interface NavigateAction extends ActionBase {
  type: "navigate";
  params: { url?: string; direction?: "back" | "forward" | "reload" };
}

export interface ClickAction extends ActionBase {
  type: "click";
  target: ElementTarget;
}

export interface TypeAction extends ActionBase {
  type: "type";
  target: ElementTarget;
  params: { text: string; clear?: boolean; submit?: boolean };
}

export interface PressAction extends ActionBase {
  type: "press";
  params: { key: string };
}

export interface ScrollAction extends ActionBase {
  type: "scroll";
  params: { direction: "up" | "down" | "top" | "bottom"; containerId?: string };
}

export interface ZoomAction extends ActionBase {
  type: "zoom";
  params: { factor?: number; direction?: "in" | "out" | "reset" };
}

export interface WaitAction extends ActionBase {
  type: "wait";
  target?: ElementTarget;
}

export interface ObserveAction extends ActionBase {
  type: "observe";
}

export interface SelectTabAction extends ActionBase {
  type: "select_tab";
  params: { tabId: number };
}

export interface NewTabAction extends ActionBase {
  type: "new_tab";
  params: { url: string };
}

export interface CloseTabAction extends ActionBase {
  type: "close_tab";
  params: { tabId: number };
}

export type BrowserAction =
  | NavigateAction
  | ClickAction
  | TypeAction
  | PressAction
  | ScrollAction
  | ZoomAction
  | WaitAction
  | ObserveAction
  | SelectTabAction
  | NewTabAction
  | CloseTabAction;

export interface InteractiveElement {
  targetId: string;
  role: string;
  name: string;
  tag: string;
  inputType?: string;
  /** Explicit search semantic from the observer (not a DOM attribute change). */
  search?: boolean;
  href?: string;
  /** Sensitive fields (password/token) report undefined — never a value. */
  value?: string;
  visible: boolean;
  enabled: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
}

export interface TabInfo {
  tabId: number;
  url: string;
  title: string;
  active: boolean;
}

export interface BrowserObservation {
  tabId: number;
  url: string;
  title: string;
  interactive: InteractiveElement[];
  scroll: { y: number; viewport: { w: number; h: number } };
  dpr: number;
  screenshot?: string;
  tabs?: TabInfo[];
  pageText?: string;
}

export interface ActionResult {
  ok: boolean;
  targetId?: string;
  observation?: BrowserObservation;
  error?: AgentError;
  evidence?: string;
}

export type TaskState =
  | "planning"
  | "awaiting_confirmation"
  | "running"
  | "awaiting_input"
  | "completed"
  | "failed"
  | "cancelled";

export interface TaskProgress {
  taskId: string;
  taskState: TaskState;
  stepsDone: number;
  message: string;
}

export interface PrivacyDecision {
  send: boolean;
  reason: string;
  violations: string[];
}

export interface PermissionRequest {
  kind: "confirmation";
  action: BrowserAction;
  message: string;
}

export interface AgentError {
  code:
    | "STALE_TARGET"
    | "TIMEOUT"
    | "NAVIGATION_FAILED"
    | "PRIVACY_VIOLATION"
    | "PLAN_INVALID"
    | "BUDGET_EXCEEDED"
    | "CANCELLED"
    | "EXTENSION_DISCONNECTED"
    | "TAB_UNAVAILABLE"
    | "TAB_UNAUTHORIZED"
    | "AMBIGUOUS_TAB"
    | "UNKNOWN";
  message: string;
  recoverable: boolean;
}

export interface PlannedStep {
  action: BrowserAction;
  why: string;
}

export interface TaskPlan {
  goal: string;
  steps: PlannedStep[];
}

export interface UserTask {
  id: string;
  instruction: string;
  status: TaskState;
}

export function validateAction(
  action: unknown
): { ok: true } | { ok: false; error: string } {
  if (typeof action !== "object" || action === null) {
    return { ok: false, error: "action is not an object" };
  }
  const a = action as Record<string, unknown>;
  const type = a.type as string | undefined;
  if (!type || !ACTION_TYPES.includes(type as ActionType)) {
    return { ok: false, error: `unknown action type: ${String(type)}` };
  }
  const risk = a.risk as RiskClass | undefined;
  if (!risk || !RISK_CLASSES.includes(risk)) {
    return { ok: false, error: `invalid risk class: ${String(risk)}` };
  }
  const floor = TYPE_ACTION_RISK_FLOOR[type as ActionType];
  if (RISK_ORDER[risk] < RISK_ORDER[floor]) {
    return {
      ok: false,
      error: `risk "${risk}" below floor "${floor}" for action "${type}"`,
    };
  }
  if (typeof a.verify !== "string" || a.verify.length === 0) {
    return { ok: false, error: "missing verify criteria" };
  }
  if (typeof a.timeoutMs !== "number" || !Number.isFinite(a.timeoutMs) || a.timeoutMs <= 0) {
    if (typeof (a as any).timeout_ms === "number" && (a as any).timeout_ms > 0) {
      a.timeoutMs = (a as any).timeout_ms;
    } else {
      a.timeoutMs = 10000;
    }
  }
  // Keep the executor inside the same bounds the planner enforces.
  a.timeoutMs = Math.min(Math.max(a.timeoutMs as number, 100), 60000);

  // Python Pydantic's model_dump() serializes every unset optional field as an
  // explicit JSON null (not omitted). Normalize before validation so null is
  // treated the same as undefined and never stringified as "null".
  const params = normalizeOptionalFields(a.params);
  const target = normalizeOptionalFields(a.target);
  if (params) a.params = params;
  else delete a.params;
  if (target) a.target = target;
  else delete a.target;

  const targetCoords = normalizeOptionalFields(target?.coords);
  if (targetCoords && target) target.coords = targetCoords;

  const hasTarget = !!target && (
    typeof target.targetId === "string" && target.targetId.length > 0
    || typeof target.name === "string" && target.name.length > 0
    || (!!targetCoords
      && typeof targetCoords.x === "number" && Number.isFinite(targetCoords.x)
      && typeof targetCoords.y === "number" && Number.isFinite(targetCoords.y))
  );

  switch (type as ActionType) {
    case "navigate": {
      if (!params) return { ok: false, error: "navigate requires params" };
      const dir = params.direction;
      const url = params.url;
      if (isAbsent(dir) && isAbsent(url)) {
        return { ok: false, error: "navigate requires url or direction" };
      }
      if (!isAbsent(dir) && !["back", "forward", "reload"].includes(String(dir))) {
        return { ok: false, error: `invalid navigate direction: ${String(dir)}` };
      }
      if (!isAbsent(url) && !isSafeHttpUrl(String(url))) {
        return { ok: false, error: `unsafe navigate url: ${String(url)}` };
      }
      break;
    }
    case "click":
      if (!hasTarget) return { ok: false, error: "click requires a target" };
      break;
    case "type":
      if (!hasTarget) return { ok: false, error: "type requires a target" };
      if (!params || typeof params.text !== "string") {
        return { ok: false, error: "type requires params.text" };
      }
      if (params.text.length > 20_000) {
        return { ok: false, error: "params.text too large" };
      }
      break;
    case "press":
      if (!params || typeof params.key !== "string" || params.key.trim().length === 0) {
        return { ok: false, error: "press requires params.key" };
      }
      break;
    case "scroll":
      if (!params || !["up", "down", "top", "bottom"].includes(String(params.direction))) {
        return { ok: false, error: "scroll requires a valid direction" };
      }
      if (!isAbsent(params.containerId) && typeof params.containerId !== "string") {
        return { ok: false, error: "containerId must be a string" };
      }
      break;
    case "zoom": {
      if (!params) return { ok: false, error: "zoom requires params" };
      const dir = params.direction;
      const factor = params.factor;
      if (isAbsent(dir) && isAbsent(factor)) {
        return { ok: false, error: "zoom requires direction or factor" };
      }
      if (!isAbsent(dir) && !["in", "out", "reset"].includes(String(dir))) {
        return { ok: false, error: `invalid zoom direction: ${String(dir)}` };
      }
      if (!isAbsent(factor) && (typeof factor !== "number" || !Number.isFinite(factor) || factor < 0.25 || factor > 3)) {
        return { ok: false, error: "zoom factor must be between 0.25 and 3" };
      }
      break;
    }
    case "select_tab":
    case "close_tab": {
      const tabId = params?.tabId;
      if (typeof tabId !== "number" || !Number.isInteger(tabId) || tabId < 0) {
        return { ok: false, error: `${type} requires a non-negative params.tabId` };
      }
      break;
    }
    case "new_tab":
      if (!params || !isSafeHttpUrl(String(params.url ?? ""))) {
        return { ok: false, error: "new_tab requires a safe http(s) url" };
      }
      break;
    case "wait":
    case "observe":
      break;
  }
  return { ok: true };
}

/**
 * JSON `null` and an omitted key both mean "not provided" in this contract.
 * Pydantic emits nulls for unset optionals, so tests and live payloads differ
 * only in representation — never in meaning.
 */
function isAbsent(value: unknown): boolean {
  return value === undefined || value === null;
}

/**
 * Copy an action's params/target object without null-valued keys.
 *
 * This is deliberately non-recursive except for `target.coords`; action
 * params are flat. Non-object values (including arrays) are treated as absent
 * so a malformed payload can never masquerade as a params object.
 */
function normalizeOptionalFields(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return undefined;
  }
  const out: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (entry === null) continue;
    out[key] = entry;
  }
  return out;
}

function isSafeHttpUrl(url: string): boolean {
  if (!/^https?:\/\//i.test(url.trim()) || /[\r\n\t\\]/.test(url)) return false;
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    if (!u.hostname) return false;
    if (u.username || u.password) return false;
    return true;
  } catch {
    return false;
  }
}
