/**
 * Shared action/observation contracts — the TS source of truth.
 * backend/app/contracts.py mirrors these; a parity test pins drift.
 */

// Task 14: standards actions. webmcp_tool calls a page-declared
// WebMCP tool in the tab; ucp_checkout runs a UCP checkout session
// — executed by the backend, never sent to the extension.
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
  "switch_tab",
  "new_tab",
  "close_tab",
  "select_option",
  "set_date",
  "upload_file",
  "webmcp_tool",
  "ucp_checkout",
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
  // Task 9: switch_tab is the planner-facing tab-switch action; it
  // behaves exactly like select_tab (kept for back-compat).
  switch_tab: "read",
  new_tab: "write",
  close_tab: "write",
  select_option: "write",
  set_date: "write",
  upload_file: "sensitive",
  // Task 14: a WebMCP tool call can change site state (the page's own
  // description of "read-only" is untrusted), so it floors at write;
  // a UCP checkout moves money, so it floors at sensitive.
  webmcp_tool: "write",
  ucp_checkout: "sensitive",
};

export interface ElementTarget {
  targetId: string;
  /** Task 7: element reference from the observation's accessibility
   * tree (e.g. "e12"). The planner quotes it; the backend resolves it
   * to a live target just before execution. A raw CSS selector is
   * never an address — the planner must not invent one. */
  elementRef?: string;
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

export interface SwitchTabAction extends ActionBase {
  type: "switch_tab";
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

export interface SelectOptionAction extends ActionBase {
  type: "select_option";
  target: ElementTarget;
  params: { option: string };
}

export interface SetDateAction extends ActionBase {
  type: "set_date";
  target: ElementTarget;
  params: { date: string }; // ISO 8601 calendar date
}

export interface UploadFileAction extends ActionBase {
  type: "upload_file";
  target: ElementTarget;
  params: { fileName: string; base64: string }; // user-provided, approval gated, max 5 MiB
}

/** Task 14: call a page-declared WebMCP tool (name + JSON arguments).
 * The backend validates the tool against the current observation and
 * the checker scans its description before this is ever dispatched. */
export interface WebMCPToolAction extends ActionBase {
  type: "webmcp_tool";
  params: { tool: string; arguments?: Record<string, unknown> };
}

/** Task 14: run a UCP checkout session. Executed by the backend, never
 * sent to the extension — present in the union so the contract stays
 * byte-identical with the backend; the executor rejects it if it ever
 * arrives here. Amount is a decimal string; the backend parses it. */
export interface UCPCheckoutAction extends ActionBase {
  type: "ucp_checkout";
  params: { merchant: string; amount: string; currency: string };
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
  | SwitchTabAction
  | NewTabAction
  | CloseTabAction
  | SelectOptionAction
  | SetDateAction
  | UploadFileAction
  | WebMCPToolAction
  | UCPCheckoutAction;

export interface SemanticElement {
  elementId: string;
  role: string;
  semanticName: string;
  /** Pre-glyph-strip accessible name, when it differs. Debugging only. */
  rawSemanticName?: string;
  controlType: "search_input" | "custom_dropdown" | "calendar_picker" | "file_uploader" |
    "toggle_switch" | "checkbox" | "radio" | "modal" | "generic";
  interactability: {
    bounds: { x: number; y: number; width: number; height: number };
    visible: boolean;
    opacity: number;
    pointerEvents: string;
    occluded: boolean;
  };
  contextText: string;
}

export interface InteractiveElement {
  semantic?: SemanticElement;
  targetId: string;
  /** Task 7: element reference from this observation's accessibility
   * tree (e.g. "e12"), assigned in observation order. */
  elementRef?: string;
  role: string;
  name: string;
  /** Pre-glyph-strip accessible name, when it differs. Debugging only. */
  rawName?: string;
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
  /** Task 7: element state (checked/selected/expanded) from ARIA/DOM. */
  state?: { checked?: boolean; selected?: boolean; expanded?: boolean };
  /** Task 7: observer's stable locator (author id or data-navigator-id). */
  cssSelector?: string;
}

/** Task 7: one grounded accessibility-tree node, mirroring backend/app/contracts.py AxNode. */
export interface AxNode {
  ref: string;
  role: string;
  name: string;
  /** Pre-glyph-strip accessible name, when it differs. Debugging only. */
  rawName?: string;
  tag: string;
  inputType?: string;
  value?: string;
  targetId: string;
  cssSelector: string;
  xpath: string;
  disabled: boolean;
  checked?: boolean;
  selected?: boolean;
  expanded?: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
  href?: string;
  source: string;
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
  /** Task 7: grounded accessibility tree for this observation. The
   * backend derives the same tree when an older extension omits it. */
  axTree?: AxNode[];
  /** Task 14: page-declared WebMCP tools for this observation (raw
   * declarations — the backend validates them before the planner
   * sees them). */
  webmcpTools?: WebMCPTool[];
  /** Task 14: UCP profile URL the page declares via <link rel="ucp">,
   * if any (same-origin only). */
  ucpProfileUrl?: string;
  /** Product image the page declares via <meta property="og:image">,
   * if any (absolute http(s) URL). The backend validates it before the
   * planner or the workspace ever renders it, and the workspace only
   * renders it for a product the agent actually visited. */
  ogImage?: string;
}

/** Task 14: one page-declared WebMCP tool, mirroring
 * backend/app/webmcp.py WebMCPTool. */
export interface WebMCPTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  outputSchema?: Record<string, unknown>;
}

export interface ActionResult {
  ok: boolean;
  targetId?: string;
  observation?: BrowserObservation;
  error?: AgentError;
  evidence?: string;
  verification?: {
    targetConfidence?: number;
    dispatchAcknowledged?: boolean;
    surfaceChanged?: boolean;
    transportStatus?: number;
    authoritativeConfirmation?: boolean;
    goalAligned?: boolean;
  };
}

export type TaskState =
  | "planning"
  | "awaiting_confirmation"
  | "running"
  | "awaiting_input"
  | "awaiting_takeover"
  | "completed"
  | "failed"
  | "cancelled"
  | "paused";

export interface AgentError {
  code:
    | "STALE_TARGET"
    | "TIMEOUT"
    | "NAVIGATION_FAILED"
    | "PRIVACY_VIOLATION"
    | "PLAN_INVALID"
    | "BUDGET_EXCEEDED"
    | "CANCELLED"
    // Task 4: the operator has the browser (takeover); the action was not
    // executed and is recoverable once the takeover resolves.
    | "TAKEOVER_ACTIVE"
    | "EXTENSION_DISCONNECTED"
    | "TAB_UNAVAILABLE"
    | "TAB_UNAUTHORIZED"
    | "AMBIGUOUS_TAB"
    // Task 14: the page exposes no usable WebMCP API, or the tool
    // call failed inside the page. Recoverable — the loop re-plans
    // and can fall back to driving the UI.
    | "WEBMCP_UNAVAILABLE"
    | "UNKNOWN";
  message: string;
  recoverable: boolean;
}

export interface PlannedStep {
  action: BrowserAction;
  why: string;
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
    || typeof target.elementRef === "string" && /^e\d+$/.test(target.elementRef)
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
    case "select_option":
      if (!hasTarget || !params || typeof params.option !== "string" || !params.option.trim() || params.option.length > 300) {
        return { ok: false, error: "select_option requires a target and short option" };
      }
      break;
    case "set_date": {
      const date = params?.date;
      if (!hasTarget || typeof date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(date) ||
          Number.isNaN(Date.parse(`${date}T00:00:00Z`)) || new Date(`${date}T00:00:00Z`).toISOString().slice(0, 10) !== date) {
        return { ok: false, error: "set_date requires a target and valid YYYY-MM-DD date" };
      }
      break;
    }
    case "upload_file": {
      const filename = params?.fileName;
      const data = params?.base64;
      if (!hasTarget || typeof filename !== "string" || !/^[\w .()-]{1,120}\.(?:pdf|png|jpe?g|txt|csv|docx|xlsx)$/i.test(filename)
          || typeof data !== "string" || data.length > 7_000_000 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(data)
          || data.length === 0) {
        return { ok: false, error: "upload_file requires approved file, allowed extension and at most 5 MiB" };
      }
      if (Math.floor(data.length * 3 / 4) - (data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0) > 5 * 1024 * 1024) {
        return { ok: false, error: "file exceeds 5 MiB" };
      }
      break;
    }
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
    case "switch_tab":
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
