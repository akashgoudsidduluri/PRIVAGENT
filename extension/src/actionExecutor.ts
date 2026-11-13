import type {
  ActionResult,
  AgentError,
  BrowserAction,
  BrowserObservation,
  TabInfo,
} from "./contracts";
import { validateAction } from "./contracts";
import { classifySearchTarget, findTarget, type Candidate } from "./targeting";
import { sanitizeObservation } from "./observer";

/** Injected content-script API shape (see lib/content.ts). */
interface AgentElement {
  targetId: string;
  semantic?: import("./contracts").SemanticElement;
  role: string;
  name: string;
  tag: string;
  inputType?: string;
  search?: boolean;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
  href?: string;
}

interface ValueProbe {
  ok: boolean;
  value?: string;
  tag?: string;
  inputType?: string;
  editable?: boolean;
  error?: string;
}

interface PageStateProbe {
  url: string;
  title: string;
  readyState: string;
  textLength: number;
  textHash: number;
  interactiveCount: number;
  resultCount: number;
}

interface AgentApi {
  list: () => Promise<AgentElement[]>;
  click: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  clickCoords: (x: number, y: number) => Promise<{ ok: boolean; targetId?: string; error?: string }>;
  getCoords: (targetId: string, name?: string) => Promise<{ x: number; y: number; width: number; height: number } | null>;
  clickPoint: (targetId: string, name?: string) => Promise<{ x: number; y: number; width: number; height: number } | null>;
  type: (targetId: string, text: string, clear?: boolean, name?: string) => Promise<{ ok: boolean; error?: string; value?: string }>;
  submit: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  readValue: (targetId: string, name?: string) => Promise<ValueProbe>;
  pageState: () => Promise<PageStateProbe>;
  scroll: (d: "up" | "down" | "top" | "bottom", containerId?: string) => Promise<{ ok: boolean; scrollY: number }>;
  extractText: (maxLen?: number) => Promise<string>;
  selectOption: (targetId: string, option: string, name?: string) => Promise<{ ok: boolean; error?: string; value?: string }>;
  setDate: (targetId: string, date: string, name?: string) => Promise<{ ok: boolean; error?: string; value?: string }>;
  uploadFile: (targetId: string, fileName: string, base64: string, name?: string) => Promise<{ ok: boolean; error?: string; value?: string }>;
}

export async function withAgent(tabId: number): Promise<AgentApi> {
  // Reuse the content script when it is already injected. Re-injecting the
  // whole bundle on every list/click/observe added a measurable round trip to
  // each step; a cheap presence probe is enough, and `list()` re-registers
  // element ids after any SPA re-render.
  let injected = false;
  try {
    const [probe] = await execScript({
      target: { tabId },
      func: () => Boolean((window as unknown as { __navigatorAgent?: unknown }).__navigatorAgent),
    }, 1_500);
    injected = Boolean(probe?.result);
  } catch {
    injected = false;
  }
  if (!injected) {
    await execScript({
      target: { tabId },
      files: ["content.js"],
    }, 3_000);
  }
  try {
    const [cProbe] = await execScript({
      target: { tabId },
      func: () => Boolean(document.querySelector("privagent-companion")),
    }, 1_500);
    if (!cProbe?.result) {
      await execScript({ target: { tabId }, files: ["companion.js"] }, 3_000);
    }
  } catch {}
  // Main-world instrumentation sees page fetch/XHR; isolated content scripts
  // cannot observe in-flight requests from React applications.
  if (!injected) try {
    await execScript({ target: { tabId }, world: "MAIN", func: () => {
      const w = window as any;
      if (w.__privagentNetwork) return;
      const state = { pending: 0, lastActivity: Date.now(), lastStatus: null as number | null };
      Object.defineProperty(w, "__privagentNetwork", { value: state, configurable: false });
      const originalFetch = w.fetch;
      w.fetch = function (...args: any[]) {
        state.pending++;
        state.lastStatus = null;
        state.lastActivity = Date.now();
        try {
          return originalFetch.apply(this, args).then((response: Response) => {
            state.lastStatus = response.status;
            return response;
          }).finally(() => { state.pending--; state.lastActivity = Date.now(); });
        } catch (error) { state.pending--; state.lastActivity = Date.now(); throw error; }
      };
      const originalSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.send = function (...args: any[]) {
        state.pending++;
        state.lastStatus = null;
        state.lastActivity = Date.now();
        this.addEventListener("loadend", () => {
          state.pending--;
          state.lastStatus = this.status;
          state.lastActivity = Date.now();
        }, { once: true });
        try { return originalSend.apply(this, args as [XMLHttpRequestBodyInit | null | undefined]); }
        catch (error) { state.pending--; state.lastActivity = Date.now(); throw error; }
      };
    } }, 250);
  } catch { /* Network evidence stays unknown if page instrumentation is blocked. */ }
  return {
    list: async () => {
      const [r] = await execScript({
        target: { tabId },
        func: () => (window as unknown as { __navigatorAgent?: { list: () => AgentElement[] } }).__navigatorAgent?.list() ?? [],
      });
      return (r?.result as AgentElement[]) ?? [];
    },
    getCoords: async (targetId: string, name?: string) => {
      const [r] = await execScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { getCoords: (i: string, n?: string) => { x: number; y: number; width: number; height: number } | null } }).__navigatorAgent?.getCoords(id, nm) ?? null,
        args: [targetId ?? "", name ?? ""],
      });
      return (r?.result as { x: number; y: number; width: number; height: number } | null) ?? null;
    },
    clickPoint: async (targetId: string, name?: string) => {
      const [r] = await execScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { getClickPoint: (i: string, n?: string) => { x: number; y: number; width: number; height: number } | null } }).__navigatorAgent?.getClickPoint(id, nm) ?? null,
        args: [targetId ?? "", name ?? ""],
      });
      return (r?.result as { x: number; y: number; width: number; height: number } | null) ?? null;
    },
    click: async (targetId: string, name?: string) => {
      const [r] = await execScript({
        target: { tabId },
        func: (id: string, nm?: string) => (window as unknown as { __navigatorAgent?: { click: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.click(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId ?? "", name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    clickCoords: async (x: number, y: number) => {
      if (typeof x !== "number" || typeof y !== "number" || !Number.isFinite(x) || !Number.isFinite(y)) {
        return { ok: false, error: "invalid coordinates" };
      }
      const [r] = await execScript({
        target: { tabId },
        func: (cx: number, cy: number) => (window as unknown as { __navigatorAgent?: { clickCoords: (px: number, py: number) => { ok: boolean; targetId?: string; error?: string } } }).__navigatorAgent?.clickCoords(cx, cy) ?? { ok: false, error: "no agent" },
        args: [x, y],
      });
      return (r?.result as { ok: boolean; targetId?: string; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    type: async (targetId: string, text: string, clear?: boolean, name?: string) => {
      const [r] = await execScript({
        target: { tabId },
        func: (id: string, val: string, cl: boolean, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { type: (i: string, v: string, c: boolean, n?: string) => { ok: boolean; error?: string; value?: string } } }).__navigatorAgent?.type(id, val, cl, nm) ?? { ok: false, error: "no agent" },
        args: [targetId ?? "", text ?? "", Boolean(clear ?? true), name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string; value?: string }) ?? { ok: false, error: "execution failed" };
    },
    submit: async (targetId: string, name?: string) => {
      const [r] = await execScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { submit: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.submit(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId ?? "", name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    readValue: async (targetId: string, name?: string) => {
      const [r] = await execScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { readValue: (i: string, n?: string) => ValueProbe } }).__navigatorAgent?.readValue(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId ?? "", name ?? ""],
      });
      return (r?.result as ValueProbe) ?? { ok: false, error: "execution failed" };
    },
    pageState: async () => {
      const [r] = await execScript({
        target: { tabId },
        func: () =>
          (window as unknown as { __navigatorAgent?: { pageState: () => PageStateProbe } }).__navigatorAgent?.pageState() ?? null,
      });
      return (r?.result as PageStateProbe) ?? {
        url: "", title: "", readyState: "", textLength: 0, textHash: 0,
        interactiveCount: 0, resultCount: 0,
      };
    },
    scroll: async (direction: "up" | "down" | "top" | "bottom", containerId?: string) => {
      const [r] = await execScript({
        target: { tabId },
        func: (dir: string, cid?: string) => (window as unknown as { __navigatorAgent?: { scroll: (d: any, c?: string) => { ok: boolean; scrollY: number } } }).__navigatorAgent?.scroll(dir as any, cid) ?? { ok: false, scrollY: 0 },
        args: [direction ?? "down", containerId ?? ""],
      });
      return (r?.result as { ok: boolean; scrollY: number }) ?? { ok: false, scrollY: 0 };
    },
    extractText: async (maxLen = 3000) => {
      const limit = typeof maxLen === "number" && Number.isFinite(maxLen) ? Math.max(100, maxLen) : 3000;
      const [r] = await execScript({
        target: { tabId },
        func: (lim: number) =>
          (window as unknown as { __navigatorAgent?: { extractText?: (m?: number) => string } }).__navigatorAgent?.extractText?.(lim) ?? "",
        args: [limit],
      });
      return (r?.result as string) ?? "";
    },
    selectOption: async (id, option, name) => {
      const [r] = await execScript({ target: { tabId }, func: (i: string, o: string, n?: string) =>
        (window as any).__navigatorAgent?.selectOption(i, o, n) ?? { ok: false, error: "no agent" }, args: [id, option, name ?? ""] }, 3_000);
      return r?.result ?? { ok: false, error: "execution failed" };
    },
    setDate: async (id, date, name) => {
      const [r] = await execScript({ target: { tabId }, func: (i: string, d: string, n?: string) =>
        (window as any).__navigatorAgent?.setDate(i, d, n) ?? { ok: false, error: "no agent" }, args: [id, date, name ?? ""] }, 5_000);
      return r?.result ?? { ok: false, error: "execution failed" };
    },
    uploadFile: async (id, fileName, base64, name) => {
      const [r] = await execScript({ target: { tabId }, func: (i: string, f: string, b: string, n?: string) =>
        (window as any).__navigatorAgent?.uploadFile(i, f, b, n) ?? { ok: false, error: "no agent" }, args: [id, fileName, base64, name ?? ""] }, 5_000);
      return r?.result ?? { ok: false, error: "execution failed" };
    },
  };
}

const CONSOLE_PORTS = new Set(["5173", "4173"]);

/** True for the NAVIGATOR console itself, which the agent must never drive. */
export function isConsoleUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return (u.hostname === "localhost" || u.hostname === "127.0.0.1" || u.hostname === "[::1]")
      && CONSOLE_PORTS.has(u.port || (u.protocol === "https:" ? "443" : "80"));
  } catch {
    return false;
  }
}

/**
 * Can the agent drive this tab?
 *
 * Deliberately broader than `isBlockedUrl`: a brand-new tab reports
 * `about:blank` (or an empty URL while committing) and *is* a legitimate place
 * to start a mission, even though `about:blank` must never be a navigation
 * target. Console/privileged pages remain excluded.
 */
export function isDrivableTabUrl(url: string): boolean {
  const u = (url || "").trim().toLowerCase();
  if (!u) return true;
  if (u === "about:blank") return true;
  if (isConsoleUrl(url)) return false;
  return !isBlockedUrl(url);
}

async function activeTab(): Promise<number | null> {
  const usable = (t: chrome.tabs.Tab) => t.id !== undefined && isDrivableTabUrl(t.url ?? "");
  // Prefer the tab in the last focused window, then any active tab
  try {
    const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (active && usable(active)) {
      return active.id!;
    }
  } catch {}
  try {
    const [active] = await chrome.tabs.query({ active: true });
    if (active && usable(active)) {
      return active.id!;
    }
  } catch {}
  // Otherwise fall back to the most recently used permitted tab.
  const all = await chrome.tabs.query({}).catch(() => [] as chrome.tabs.Tab[]);
  const candidates = all
    .filter(usable)
    .sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0));
  return candidates[0]?.id ?? null;
}

/**
 * Return a drivable tab, creating one when the browser has nothing usable
 * (only the NAVIGATOR console, chrome:// pages, or no window at all).
 *
 * Without this, every action aborted with a non-recoverable
 * "no active tab" before the planner could navigate anywhere.
 */
export async function getOrCreateActiveTab(defaultUrl = "about:blank"): Promise<number> {
  const existing = await activeTab();
  if (existing !== null) return existing;
  let tabId: number | undefined;
  try {
    const created = await chrome.tabs.create({ url: defaultUrl, active: true });
    tabId = created.id;
  } catch {
    try {
      const win = await chrome.windows.create({ url: defaultUrl, focused: true });
      tabId = win?.tabs?.[0]?.id;
    } catch {}
  }
  if (tabId === undefined) {
    const win = await chrome.windows.create({ url: defaultUrl, focused: true });
    tabId = win?.tabs?.[0]?.id;
  }
  if (tabId === undefined) {
    throw new Error("could not create a browser tab");
  }
  if (defaultUrl !== "about:blank") {
    await waitForStable(tabId, 5000, defaultUrl);
  }
  return tabId;
}

export interface ExtensionSession {
  sessionId: string;
  primaryTabId: number;
  activeTabId?: number;
  childTabIds: Set<number>;
  origin: "new_task" | "existing_user_tab" | "reused_session";
  createdAt: number;
  lastActivityAt: number;
}

export const extensionSessions = new Map<string, ExtensionSession>();

export function clearExtensionSessions(): void {
  extensionSessions.clear();
}

if (typeof chrome !== "undefined" && chrome?.tabs?.onRemoved?.addListener) {
  try {
    chrome.tabs.onRemoved.addListener((tabId) => {
      for (const sess of extensionSessions.values()) {
        if (sess.childTabIds.has(tabId)) {
          sess.childTabIds.delete(tabId);
        }
      }
    });
  } catch {}
}

export async function initSession(params: {
  sessionId: string;
  useExistingTab?: boolean;
  tabHint?: string;
  tabId?: number;
}): Promise<ActionResult & { primaryTabId?: number }> {
  const { sessionId, useExistingTab, tabHint, tabId } = params;

  if (extensionSessions.has(sessionId)) {
    const existing = extensionSessions.get(sessionId)!;
    existing.lastActivityAt = Date.now();
    return {
      ok: true,
      evidence: `session ${sessionId} resumed with primaryTabId ${existing.primaryTabId}`,
      primaryTabId: existing.primaryTabId,
    };
  }

  let resolvedTabId: number | undefined;

  if (useExistingTab) {
    const all = await chrome.tabs.query({}).catch(() => [] as chrome.tabs.Tab[]);
    const drivable = all.filter(t => t.id !== undefined && isDrivableTabUrl(t.url ?? ""));

    if (tabId !== undefined) {
      const match = drivable.find(t => t.id === tabId);
      if (!match) {
        return {
          ok: false,
          error: { code: "TAB_UNAVAILABLE", message: `Specified tab ${tabId} not found or not drivable`, recoverable: false },
        };
      }
      resolvedTabId = tabId;
    } else if (tabHint) {
      const hintLower = tabHint.toLowerCase().trim();
      const matchingTabs = drivable.filter(t => {
        const u = (t.url ?? "").toLowerCase();
        const title = (t.title ?? "").toLowerCase();
        return u.includes(hintLower) || title.includes(hintLower);
      });
      if (matchingTabs.length === 0) {
        return {
          ok: false,
          error: { code: "TAB_UNAVAILABLE", message: `No active tab matches hint "${tabHint}"`, recoverable: false },
        };
      }
      if (matchingTabs.length > 1) {
        return {
          ok: false,
          error: { code: "AMBIGUOUS_TAB", message: `Multiple tabs match hint "${tabHint}" (${matchingTabs.length} found)`, recoverable: false },
        };
      }
      resolvedTabId = matchingTabs[0].id!;
    } else {
      const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true }).catch(() => [] as chrome.tabs.Tab[]);
      if (active && active.id !== undefined && isDrivableTabUrl(active.url ?? "")) {
        resolvedTabId = active.id;
      } else {
        const activeTabs = drivable.filter(t => t.active);
        if (activeTabs.length === 1) {
          resolvedTabId = activeTabs[0].id!;
        } else {
          return {
            ok: false,
            error: { code: "AMBIGUOUS_TAB", message: "Cannot uniquely identify active user tab. Specify a tab hint.", recoverable: false },
          };
        }
      }
    }
  } else {
    try {
      const created = await chrome.tabs.create({ url: "about:blank", active: true });
      resolvedTabId = created.id;
    } catch {
      try {
        const win = await chrome.windows.create({ url: "about:blank", focused: true });
        resolvedTabId = win?.tabs?.[0]?.id;
      } catch {}
    }
  }

  if (resolvedTabId === undefined) {
    return {
      ok: false,
      error: { code: "TAB_UNAVAILABLE", message: "Failed to allocate or find a browser tab for session", recoverable: false },
    };
  }

  const session: ExtensionSession = {
    sessionId,
    primaryTabId: resolvedTabId,
    childTabIds: new Set<number>(),
    origin: useExistingTab ? "existing_user_tab" : "new_task",
    createdAt: Date.now(),
    lastActivityAt: Date.now(),
  };
  extensionSessions.set(sessionId, session);

  return {
    ok: true,
    evidence: `session ${sessionId} initialized with primaryTabId ${resolvedTabId}`,
    primaryTabId: resolvedTabId,
  };
}

export async function resolveSessionTab(
  action: BrowserAction,
): Promise<{ tabId: number } | ActionResult> {
  const sessionId = action.sessionId;
  const explicitTabId = action.tabId;

  if (sessionId) {
    const session = extensionSessions.get(sessionId);
    if (!session) {
      return {
        ok: false,
        error: { code: "TAB_UNAUTHORIZED", message: `Session ${sessionId} is unknown or expired`, recoverable: false },
      };
    }

    session.lastActivityAt = Date.now();
    let targetId = explicitTabId;
    if (targetId === undefined) {
      if (session.activeTabId !== undefined && (session.activeTabId === session.primaryTabId || session.childTabIds.has(session.activeTabId))) {
        targetId = session.activeTabId;
      } else {
        targetId = session.primaryTabId;
      }
    }

    if (targetId !== session.primaryTabId && !session.childTabIds.has(targetId)) {
      return {
        ok: false,
        error: {
          code: "TAB_UNAUTHORIZED",
          message: `Tab ${targetId} is not pinned or authorized for session ${sessionId}`,
          recoverable: false,
        },
      };
    }

    const tab = await chrome.tabs.get(targetId).catch(() => null);
    if (!tab || !isDrivableTabUrl(tab.url ?? "")) {
      return {
        ok: false,
        error: {
          code: "TAB_UNAVAILABLE",
          message: `Pinned tab ${targetId} is closed or unavailable`,
          recoverable: false,
        },
      };
    }

    return { tabId: targetId };
  }

  if (explicitTabId !== undefined) {
    const tab = await chrome.tabs.get(explicitTabId).catch(() => null);
    if (!tab || !isDrivableTabUrl(tab.url ?? "")) {
      return {
        ok: false,
        error: { code: "TAB_UNAVAILABLE", message: `Tab ${explicitTabId} not available`, recoverable: false },
      };
    }
    return { tabId: explicitTabId };
  }

  const defaultTab = await getOrCreateActiveTab();
  return { tabId: defaultTab };
}


async function currentUrl(tabId: number): Promise<string> {
  const tab = await chrome.tabs.get(tabId);
  return tab?.url ?? "";
}

const sleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms));

export interface PageSettlement {
  ready: boolean;
  interactiveCount: number;
  isConnecting: boolean;
}

/**
 * Decide whether the page has finished its interactive paint.
 *
 * - Three or more controls: usable immediately.
 * - One/two controls: wait for two stable samples (~80ms).
 * - Empty page: wait for five stable samples (~200ms) instead of the old
 *   3.5s SPA penalty. Chat apps that report "Connecting…" keep waiting.
 */
export function isPageSettled(
  status: PageSettlement,
  previousInteractive = -1,
  stableSamples = 0,
): boolean {
  if (!status.ready || status.isConnecting) return false;
  if (status.interactiveCount >= 3) return true;
  const required = status.interactiveCount === 0 ? 5 : 2;
  return status.interactiveCount === previousInteractive && stableSamples >= required;
}

function samePageUrl(a: string, b: string): boolean {
  if (a === b) return true;
  try {
    const ua = new URL(a);
    const ub = new URL(b);
    const norm = (p: string) => p.replace(/\/$/, "");
    return ua.origin === ub.origin
      && norm(ua.pathname) === norm(ub.pathname)
      && ua.search === ub.search
      && ua.hash === ub.hash;
  } catch {
    return false;
  }
}

async function pageStatus(tabId: number): Promise<PageSettlement | null> {
  try {
    const [res] = await execScript({
      target: { tabId },
      func: () => {
        const text = document.body?.innerText || "";
        return {
          ready: document.readyState !== "loading",
          interactiveCount: document.querySelectorAll(
            'button, input, textarea, a[href], [role="button"], [contenteditable], [data-testid], ytd-thumbnail'
          ).length,
          isConnecting: text.includes("Connecting to WhatsApp") || text.includes("Loading your chats"),
        };
      },
    }, 1_500);
    return (res?.result as PageSettlement) ?? null;
  } catch {
    return null;
  }
}

/**
 * Resolve when DOM mutations have been quiet for `quietMs`, capped by `maxMs`.
 * A navigation that destroys the execution context rejects the injected
 * promise; callers then fall back to waitForStable().
 */
async function settleDom(tabId: number, quietMs = 120, maxMs = 700): Promise<void> {
  const qVal = typeof quietMs === "number" && Number.isFinite(quietMs) ? quietMs : 120;
  const mVal = typeof maxMs === "number" && Number.isFinite(maxMs) ? maxMs : 700;
  const quiet = Math.max(30, qVal);
  const cap = Math.max(quiet, mVal);
  try {
    await execScript({
      target: { tabId },
      func: async (q: number, capMs: number) => {
        await new Promise<void>(resolve => {
          const root = document.documentElement;
          let done = false;
          let quietTimer: ReturnType<typeof setTimeout> | undefined;
          let maxTimer: ReturnType<typeof setTimeout> | undefined;
          let observer: MutationObserver | undefined;
          const finish = () => {
            if (done) return;
            done = true;
            if (quietTimer !== undefined) clearTimeout(quietTimer);
            if (maxTimer !== undefined) clearTimeout(maxTimer);
            observer?.disconnect();
            resolve();
          };
          if (!root) {
            finish();
            return;
          }
          maxTimer = setTimeout(finish, capMs);
          try {
            observer = new MutationObserver(() => {
              if (quietTimer !== undefined) clearTimeout(quietTimer);
              quietTimer = setTimeout(finish, q);
            });
            observer.observe(root, {
              childList: true,
              subtree: true,
              characterData: true,
            });
          } catch {
            // Observation unavailable: the quiet timer below still resolves.
          }
          quietTimer = setTimeout(finish, q);
        });
      },
      args: [quiet, cap],
    }, cap + 400);
  } catch {
    // Navigation replaced the document mid-wait.
  }
  const deadline = Date.now() + cap;
  while (Date.now() < deadline) {
    try {
      const [probe] = await execScript({ target: { tabId }, world: "MAIN", func: () => {
        const state = (window as any).__privagentNetwork;
        return state ? { pending: state.pending, lastActivity: state.lastActivity } : null;
      } }, 100);
      const state = probe?.result;
      if (!state || typeof state.pending !== "number" ||
          (state.pending === 0 && Date.now() - state.lastActivity >= quiet)) break;
    } catch { break; }
    await sleep(40);
  }
}

async function networkStatusSince(tabId: number, since: number): Promise<number | undefined> {
  try {
    const [probe] = await execScript({ target: { tabId }, world: "MAIN", func: () => {
      const s = (window as any).__privagentNetwork;
      return s ? { at: s.lastActivity, status: s.lastStatus, pending: s.pending } : null;
    } }, 200);
    const state = probe?.result;
    return state && state.pending === 0 && state.at >= since &&
      Number.isInteger(state.status) && state.status >= 100 && state.status < 600
      ? state.status : undefined;
  } catch { return undefined; }
}

async function detectNewTab(tabId: number, tabIdsBefore: Set<number>, windowId?: number): Promise<chrome.tabs.Tab | null> {
  const currentTabs = await chrome.tabs.query({}).catch(() => [] as chrome.tabs.Tab[]);
  const newTabs = currentTabs.filter(t => t.id !== undefined && !tabIdsBefore.has(t.id) && isDrivableTabUrl(t.url ?? ""));
  if (newTabs.length === 0) return null;

  // 1. Direct parent/opener match: tab explicitly designated as opened by tabId
  const openerMatch = newTabs.find(t => t.openerTabId === tabId);
  if (openerMatch) return openerMatch;

  // 2. Same window match
  if (windowId !== undefined) {
    const windowMatch = newTabs.find(t => t.windowId === windowId);
    if (windowMatch) return windowMatch;
  }

  // 3. Fallback to newest drivable tab
  return newTabs[newTabs.length - 1] ?? null;
}

/**
 * Post-action settle used by click/type-submit/press. Watches for a new tab,
 * a URL change, a loading tab, or a newly activated tab (target=_blank),
 * otherwise waits for DOM quiescence. Returns the tab whose observation
 * the next step should use.
 */
async function settleAfterAction(
  tabId: number,
  beforeUrl: string,
  timeoutMs: number,
  tabIdsBefore?: Set<number>
): Promise<number> {
  // 1. Check if a newly created tab appeared (target=_blank, window.open, etc.)
  if (tabIdsBefore && tabIdsBefore.size > 0) {
    const originTab = await chrome.tabs.get(tabId).catch(() => null);
    const windowId = originTab?.windowId;
    let newTab: chrome.tabs.Tab | null = null;
    for (let i = 0; i < 10; i++) {
      newTab = await detectNewTab(tabId, tabIdsBefore, windowId);
      if (newTab && newTab.id !== undefined) break;
      await sleep(40);
    }
    if (newTab && newTab.id !== undefined) {
      const destinationTabId = newTab.id;
      await chrome.tabs.update(destinationTabId, { active: true }).catch(() => {});
      await waitForStable(destinationTabId, timeoutMs);
      return destinationTabId;
    }
  }

  // 2. Current tab settled / navigated in-place
  const cap = Math.max(150, Math.min(timeoutMs, 1200));
  await settleDom(tabId, 120, cap);

  // A navigation can start just after the click's DOM churn settles.
  for (let i = 0; i < 10; i++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.status === "loading" || (tab?.url && tab.url !== beforeUrl)) {
      await waitForStable(tabId, timeoutMs);
      return tabId;
    }
    await sleep(40);
  }
  return tabId;
}

const BLOCKED_SCHEMES = [
  "chrome://",
  "chrome-extension://",
  "chrome-search:",
  "devtools:",
  "view-source:",
  "edge://",
  "brave://",
  "opera://",
  "about:",
  "file://",
  "data:",
  "blob:",
  "javascript:",
  "vbscript:",
  "filesystem:",
];

export function isBlockedUrl(url: string): boolean {
  const u = (url || "").trim().toLowerCase();
  if (!u) return true;
  // Anything without an http(s) origin is off-limits for task navigation.
  if (!/^https?:\/\//.test(u)) return true;
  return BLOCKED_SCHEMES.some(s => u.startsWith(s));
}

export async function executeAction(action: BrowserAction, taskId: string): Promise<ActionResult> {
  const raw = action as any;
  if (raw?.type === "init_session") {
    return await initSession(raw);
  }
  if (raw?.type === "close_session") {
    const sId = raw?.sessionId;
    if (sId && extensionSessions.has(sId)) {
      extensionSessions.delete(sId);
    }
    return { ok: true, evidence: `session ${sId} closed` };
  }

  const v = validateAction(action);
  if (!v.ok) {
    return { ok: false, error: { code: "PLAN_INVALID", message: `invalid action: ${v.error}`, recoverable: false } };
  }
  try {
    if (action.type === "new_tab") {
      return await doNewTab(action);
    }
    if (action.type === "select_tab") {
      if (action.sessionId) {
        const session = extensionSessions.get(action.sessionId);
        if (session) {
          if (action.params.tabId !== session.primaryTabId && !session.childTabIds.has(action.params.tabId)) {
            return err("TAB_UNAUTHORIZED", `Tab ${action.params.tabId} is not in session ${action.sessionId}`, false);
          }
          session.activeTabId = action.params.tabId;
        }
      }
      await chrome.tabs.update(action.params.tabId, { active: true });
      await waitForStable(action.params.tabId, action.timeoutMs);
      return {
        ok: true,
        observation: await observe(action.params.tabId),
        evidence: `tab ${action.params.tabId} active`,
      };
    }
    if (action.type === "close_tab") {
      const session = action.sessionId ? extensionSessions.get(action.sessionId) : undefined;
      if (session) {
        if (action.params.tabId !== session.primaryTabId && !session.childTabIds.has(action.params.tabId)) {
          return err("TAB_UNAUTHORIZED", `Tab ${action.params.tabId} is not in session ${action.sessionId}`, false);
        }
        // Closing the primary tab would leave the session without a home tab;
        // the planner must close a child tab (or end the task) instead.
        if (action.params.tabId === session.primaryTabId) {
          return err("PLAN_INVALID", "cannot close the primary tab of an active session", false);
        }
        session.childTabIds.delete(action.params.tabId);
        if (session.activeTabId === action.params.tabId) {
          session.activeTabId = session.primaryTabId;
        }
      }
      await chrome.tabs.remove(action.params.tabId);
      let nextTabId: number;
      if (session) {
        nextTabId = session.primaryTabId;
      } else {
        nextTabId = await getOrCreateActiveTab();
      }
      return {
        ok: true,
        observation: await observe(nextTabId),
        evidence: `tab ${action.params.tabId} closed`,
      };
    }

    const resolved = await resolveSessionTab(action);
    if ("ok" in resolved && !resolved.ok) {
      return resolved;
    }
    const tabId = (resolved as { tabId: number }).tabId;

    switch (action.type) {
      case "navigate": return await doNavigate(action, tabId);
      case "click": return await doClick(action, taskId, tabId);
      case "type": return await doType(action, taskId, tabId);
      case "select_option": return await doSpecialInput(action, tabId, "selectOption");
      case "set_date": return await doSpecialInput(action, tabId, "setDate");
      case "upload_file": return await doSpecialInput(action, tabId, "uploadFile");
      case "press": return await doPress(action, tabId);
      case "scroll": return await doScroll(action, tabId);
      case "zoom": return await doZoom(action, tabId);
      case "wait": {
        await ensureTabActive(tabId);
        await new Promise(r => setTimeout(r, Math.min(action.timeoutMs, 10_000)));
        return {
          ok: true,
          observation: await observe(tabId),
          evidence: "waited for page/app to settle"
        };
      }
      case "observe": {
        return { ok: true, observation: await observe(tabId) };
      }
    }
  } catch (e) {
    return err("UNKNOWN", String(e), true);
  }
}

async function doSpecialInput(
  action: Extract<BrowserAction, { type: "select_option" | "set_date" | "upload_file" }>,
  tabId: number,
  method: "selectOption" | "setDate" | "uploadFile",
): Promise<ActionResult> {
  await ensureTabActive(tabId);
  const agent = await withAgent(tabId);
  const target = findTarget(action.target, await agent.list(), "click");
  if (!target) return err("STALE_TARGET", "special input target missing or ambiguous", true);
  const p = action.params as any;
  const result = method === "selectOption" ? await agent.selectOption(target.targetId, p.option, target.name)
    : method === "setDate" ? await agent.setDate(target.targetId, p.date, target.name)
    : await agent.uploadFile(target.targetId, p.fileName, p.base64, target.name);
  if (!result.ok) return err("STALE_TARGET", result.error ?? "input not verified", true);
  await settleDom(tabId);
  return { ok: true, targetId: target.targetId, observation: await observe(tabId),
    evidence: `${action.type} selected and read back`,
    verification: { targetConfidence: 0.85, dispatchAcknowledged: false, surfaceChanged: true } };
}

function err(code: AgentError["code"], message: string, recoverable: boolean): ActionResult {
  return { ok: false, error: { code, message, recoverable } };
}

async function doNavigate(action: Extract<BrowserAction, { type: "navigate" }>, tabId: number): Promise<ActionResult> {
  const p = action.params;
  const existing = tabId;

  // Browser-history navigation needs a tab that exists; there is nowhere to
  // go back to otherwise, so this stays a recoverable error.
  if (p.direction === "back" || p.direction === "forward" || p.direction === "reload") {
    if (p.direction === "back") { await chrome.tabs.goBack(existing); }
    else if (p.direction === "forward") { await chrome.tabs.goForward(existing); }
    else { await chrome.tabs.reload(existing); }
    const url = await waitForStable(existing, action.timeoutMs);
    return { ok: true, observation: await observe(existing), evidence: url };
  }

  if (!p.url) {
    return err("NAVIGATION_FAILED", "navigate needs url or direction", false);
  }
  if (isBlockedUrl(p.url)) {
    return err("NAVIGATION_FAILED", `blocked scheme: ${p.url}`, false);
  }

  await chrome.tabs.update(existing, { url: p.url });
  const url = await waitForStable(existing, action.timeoutMs, p.url);
  return { ok: true, observation: await observe(existing), evidence: url };
}

async function ensureTabActive(tabId: number): Promise<void> {
  if (typeof chrome === "undefined" || !chrome.tabs) return;
  try {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) return;
    // Avoid redundant activation/focus churn: focus changes are slow and
    // every observation used to pay for them even when already focused.
    if (!tab.active) {
      await chrome.tabs.update(tabId, { active: true });
    }
    if (tab.windowId && chrome.windows) {
      const win = await chrome.windows.get(tab.windowId).catch(() => null);
      if (win && !win.focused) {
        await chrome.windows.update(tab.windowId, { focused: true }).catch(() => null);
      }
    }
  } catch {}
}

/**
 * CDP virtual-key descriptors.
 *
 * `windowsVirtualKeyCode` is what React/Lexical-based editors read off the
 * event; without it their keydown handlers treat the press as unknown.
 */
const CDP_KEYS = {
  Enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, text: "\r", unmodifiedText: "\r" },
  Backspace: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8 },
  A: { key: "a", code: "KeyA", windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65 },
} as const;

const CTRL_MODIFIER = 2;
const META_MODIFIER = 4;

export type CdpKeyIntent = "enter" | "backspace" | "selectAll";

/**
 * Build the raw CDP key events for one intent.
 *
 * Exported for tests: the modifier bitmask and the rawKeyDown/keyUp pairing are
 * the parts that silently break native editing if they drift.
 */
export function cdpKeySequence(
  intent: CdpKeyIntent,
  isMac = false,
): Array<Record<string, unknown>> {
  const modifiers = intent === "selectAll" ? (isMac ? META_MODIFIER : CTRL_MODIFIER) : 0;
  const desc =
    intent === "enter" ? CDP_KEYS.Enter
    : intent === "backspace" ? CDP_KEYS.Backspace
    : CDP_KEYS.A;
  const base: Record<string, unknown> = {
    modifiers,
    key: desc.key,
    code: desc.code,
    windowsVirtualKeyCode: desc.windowsVirtualKeyCode,
    nativeVirtualKeyCode: desc.nativeVirtualKeyCode,
  };
  if ("text" in desc) {
    base.text = desc.text;
    base.unmodifiedText = desc.unmodifiedText;
  }
  // rawKeyDown fires the page's keydown/default action; keyUp mirrors it so
  // editors never see a stuck modifier or a half-pressed key.
  return [{ type: "rawKeyDown", ...base }, { type: "keyUp", ...base }];
}

function isMacPlatform(): boolean {
  try {
    return /Mac/i.test(navigator.platform || navigator.userAgent || "");
  } catch {
    return false;
  }
}

// Chrome can leave a debugger promise pending after a navigation or when
// another client owns the target. An unbounded await there stalls the whole
// action until the relay's deadline fires, losing a successful interaction.
const DEBUGGER_ATTACH_MS = 2_000;
const DEBUGGER_COMMAND_MS = 3_000;
const DEBUGGER_DETACH_MS = 1_200;

async function withDebuggerDeadline<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  try {
    return await Promise.race([promise, deadline]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

async function debugSend(
  target: { tabId: number },
  method: string,
  params?: Record<string, unknown>,
): Promise<unknown> {
  return await withDebuggerDeadline(
    chrome.debugger.sendCommand(target, method, params),
    DEBUGGER_COMMAND_MS,
    `debugger ${method}`,
  );
}

/**
 * Bound page injections. When a tab starts navigating mid-injection Chrome may
 * never settle the returned promise, which would wedge the action until the
 * relay deadline. A rejected injection is always handled by the caller as an
 * unavailable page, so a hard timeout is safe here.
 */
async function execScript(injection: any, ms = 5_000): Promise<any[]> {
  return await withDebuggerDeadline(
    chrome.scripting["executeScript"](injection) as Promise<any[]>,
    ms,
    "executeScript",
  );
}

async function debugDetach(target: { tabId: number }): Promise<void> {
  try {
    await withDebuggerDeadline(
      chrome.debugger.detach(target),
      DEBUGGER_DETACH_MS,
      "debugger detach",
    );
  } catch {
    // Detach is best-effort: a tab that navigated mid-action may already have
    // dropped the session, and a stuck detach must not hide a real result.
  }
}

interface DebuggerAttachResult {
  ok: boolean;
  /** True when this call owns the session and must detach it afterwards. */
  owned: boolean;
  error?: string;
}

async function debugAttach(target: { tabId: number }): Promise<DebuggerAttachResult> {
  try {
    await withDebuggerDeadline(
      chrome.debugger.attach(target, "1.3"),
      DEBUGGER_ATTACH_MS,
      "debugger attach",
    );
    return { ok: true, owned: true };
  } catch (e: any) {
    const message = String(e?.message ?? e);
    if (message.includes("already attached")) {
      // Someone else owns the session; use it but never detach their client.
      return { ok: true, owned: false };
    }
    // A timed-out attach may still complete later; report ownership so the
    // caller can release it. A hard failure never attached.
    return { ok: false, owned: message.includes("timed out"), error: message };
  }
}

async function dispatchKeySequence(
  target: { tabId: number },
  sequence: Array<Record<string, unknown>>,
): Promise<void> {
  for (const event of sequence) {
    await debugSend(target, "Input.dispatchKeyEvent", event);
  }
}

/** Compare the DOM value read back after typing with what we asked for. */
export function typedValueMatches(actual: string | undefined, expected: string, clear: boolean): boolean {
  const value = actual ?? "";
  if (clear) {
    return value === expected || value.trim() === expected.trim();
  }
  return value === expected || value.endsWith(expected) || value.includes(expected);
}

interface FocusPoint {
  x: number;
  y: number;
}

interface CdpTypeOptions {
  targetId?: string;
  targetName?: string;
  /** Resolve a search/send button while the same debugger session is attached. */
  submit?: () => Promise<FocusPoint | null>;
  /** Re-resolve the input rectangle after an SPA rerender. */
  refocus?: () => Promise<FocusPoint | null>;
  /** Snapshot search state after typing, immediately before submission. */
  beforeSubmit?: () => Promise<PageStateProbe | null>;
}

interface CdpTypeResult {
  typed: boolean;
  verified: boolean;
  actualValue?: string;
  submitted: boolean;
  submitMethod?: "button" | "enter";
  /** Why the native path could not verify, for honest fallback evidence. */
  error?: string;
}

async function cdpMouseClick(target: { tabId: number }, coords: FocusPoint): Promise<void> {
  const x = Math.round(coords.x);
  const y = Math.round(coords.y);
  await debugSend(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await debugSend(target, "Input.dispatchMouseEvent", {
    type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1, pointerType: "mouse",
  });
  await debugSend(target, "Input.dispatchMouseEvent", {
    type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1, pointerType: "mouse",
  });
}

function valueProbeExpression(targetId?: string, targetName?: string): string {
  const tid = JSON.stringify(targetId || "");
  const name = JSON.stringify((targetName || "").trim().toLowerCase());
  return `(() => {
    const read = (e) => e ? (e.isContentEditable ? (e.textContent || "") : ('value' in e ? String(e.value) : (e.textContent || ""))) : "";
    const tid = ${tid};
    const nm = ${name};
    let el = null;
    try {
      if (tid) {
        el = document.querySelector('[data-navigator-id="' + CSS.escape(tid) + '"]')
          || document.getElementById(tid)
          || document.querySelector('[name="' + CSS.escape(tid) + '"]');
      }
    } catch (e) {}
    if (!el && nm) {
      const all = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"], [role="searchbox"], [role="textbox"]'));
      el = all.find((e) => {
        const label = String(e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('placeholder') || '').trim().toLowerCase();
        return label === nm || (label.length > 2 && label.includes(nm));
      }) || null;
    }
    const active = document.activeElement;
    const editable = !!active && (active.isContentEditable || active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT');
    return {
      targetFound: !!el,
      targetValue: read(el),
      activeValue: read(active),
      activeEditable: editable,
      activeTag: active ? active.tagName : '',
    };
  })()`;
}

async function cdpEvaluate(target: { tabId: number }, expression: string): Promise<any> {
  const result = await debugSend(target, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: false,
  }) as any;
  return result?.result?.value;
}

/**
 * Type (and optionally submit) through one Chrome DevTools Protocol session.
 *
 * The old flow attached, typed, detached, then attached again for Enter. That
 * detach/reattach boundary is where SPAs can drop focus or replace the input,
 * so Enter lands on <body>. Keeping one session means the verified editable is
 * still focused when submission happens. The debugger is always detached in
 * `finally`, including error paths. No React `_valueTracker` poking is done:
 * `Input.insertText` is the native editing command React already understands.
 */
async function cdpType(
  tabId: number,
  coords: FocusPoint | null,
  text: string,
  clear: boolean,
  options: CdpTypeOptions = {},
): Promise<CdpTypeResult> {
  const failed: CdpTypeResult = { typed: false, verified: false, submitted: false };
  if (typeof chrome === "undefined" || !chrome.debugger) return failed;
  // Without coordinates we cannot focus the field with trusted input, and
  // insertText would land in whatever happens to be focused instead.
  if (!coords || coords.x <= 0 || coords.y <= 0) return failed;

  await ensureTabActive(tabId);
  const target = { tabId };
  // Track any attach we initiated so a timed-out attach is still detached.
  let attachAttempted = false;
  try {
    const attach = await debugAttach(target);
    attachAttempted = attach.owned;
    if (!attach.ok) {
      return { ...failed, error: attach.error ?? "debugger attach failed" };
    }

    // A trusted click both focuses the field and places the caret, which is
    // what allows insertText to target the right editable.
    await cdpMouseClick(target, coords);

    if (clear) {
      await dispatchKeySequence(target, cdpKeySequence("selectAll", isMacPlatform()));
      await dispatchKeySequence(target, cdpKeySequence("backspace"));
    }

    await debugSend(target, "Input.insertText", { text });

    // Let React/controlled inputs flush their state, then verify the actual
    // DOM value (not just that a command was sent).
    await new Promise(r => setTimeout(r, 60));
    const probe = await cdpEvaluate(target, valueProbeExpression(options.targetId, options.targetName));
    const actualValue: string = probe?.targetFound ? probe.targetValue : probe.activeValue;
    const verified = typedValueMatches(actualValue, text, clear);
    let submitted = false;
    let submitMethod: "button" | "enter" | undefined;

    if (options.submit && verified) {
      await new Promise(r => setTimeout(r, 80));
      // Capture post-typing state inside the session so autocomplete changes
      // are not mistaken for a successful submission.
      if (options.beforeSubmit) {
        try { await options.beforeSubmit(); } catch {}
      }
      // Autocomplete often rerenders. If the editable lost focus, re-resolve
      // its rectangle and click it again inside this same session.
      if (!probe?.activeEditable && options.refocus) {
        const refreshed = await options.refocus();
        if (refreshed && refreshed.x > 0 && refreshed.y > 0) {
          await cdpMouseClick(target, refreshed);
        }
      }
      const submitPoint = await options.submit();
      if (submitPoint && submitPoint.x > 0 && submitPoint.y > 0) {
        await cdpMouseClick(target, submitPoint);
        submitted = true;
        submitMethod = "button";
      } else {
        await dispatchKeySequence(target, cdpKeySequence("enter"));
        submitted = true;
        submitMethod = "enter";
      }
    }

    return { typed: true, verified, actualValue, submitted, submitMethod };
  } catch (e) {
    return { ...failed, error: String(e instanceof Error ? e.message : e) };
  } finally {
    if (attachAttempted) {
      await debugDetach(target);
    }
  }
}

async function cdpClick(tabId: number, x: number, y: number): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  const attach = await debugAttach(target);
  if (!attach.ok) return false;

  let pressed = false;
  try {
    await debugSend(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: Math.round(x),
      y: Math.round(y),
    });
    await debugSend(target, "Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      buttons: 1,
      clickCount: 1,
      pointerType: "mouse",
    });
    // From here the page may already have received the activation. Report
    // success even if the release fails so callers never fall back to a
    // second in-page click.
    pressed = true;
    await new Promise(r => setTimeout(r, 20));
    await debugSend(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      buttons: 0,
      clickCount: 1,
      pointerType: "mouse",
    });
    await new Promise(r => setTimeout(r, 20));
    return true;
  } catch {
    return pressed;
  } finally {
    if (attach.owned) await debugDetach(target);
  }
}

async function cdpPressKey(tabId: number, key: string): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  const attach = await debugAttach(target);
  if (!attach.ok) return false;

  let down = false;
  try {
    if (key !== "Enter") return false;
    const [keyDown, keyUp] = cdpKeySequence("enter");
    await debugSend(target, "Input.dispatchKeyEvent", keyDown);
    // keyDown may already submit the form; treat a failed keyUp as a
    // successful attempt so no second Enter is dispatched.
    down = true;
    await new Promise(r => setTimeout(r, 15));
    await debugSend(target, "Input.dispatchKeyEvent", keyUp);
    return true;
  } catch {
    return down;
  } finally {
    if (attach.owned) await debugDetach(target);
  }
}

/**
 * Detect visible modal/dialog overlays and dismiss them if an unambiguous
 * safe close control (e.g. '✕', aria-label="Close", button.close, button._2doB4z) is present.
 */
export async function dismissObstructiveModalIfPresent(tabId: number): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.scripting) return false;
  try {
    const [r] = await execScript({
      target: { tabId },
      func: () => {
        const modal = document.querySelector('[role="dialog"], [aria-modal="true"], .modal, ._2KpZ6l, [class*="dialog" i], [class*="modal" i], [class*="overlay" i], [class*="popup" i]') as HTMLElement | null;
        if (!modal) return false;

        const rect = modal.getBoundingClientRect();
        if (rect.width < 50 || rect.height < 50) return false;
        const style = window.getComputedStyle(modal);
        if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;

        const closeSelectors = [
          'button._2doB4z',
          'button._2KpZ6l._2doB4z',
          'button[aria-label*="close" i]',
          'button.close',
          '[data-testid*="close"]',
          '[aria-label="Close" i]',
          '[aria-label*="dismiss" i]',
          'button[class*="close" i]',
        ];
        let closeBtn: HTMLElement | null = modal.querySelector(closeSelectors.join(', '));
        if (!closeBtn) {
          const allBtns = modal.querySelectorAll('button, span[role="button"], div[role="button"]');
          for (const btn of Array.from(allBtns)) {
            const txt = (btn.textContent || "").trim();
            if (txt === "✕" || txt === "×" || txt === "X") {
              closeBtn = btn as HTMLElement;
              break;
            }
          }
        }
        if (closeBtn) {
          closeBtn.click();
          return true;
        }
        return false;
      },
    });
    return Boolean(r?.result);
  } catch {
    return false;
  }
}

async function doClick(action: Extract<BrowserAction, { type: "click" }>, taskId: string, tabId: number): Promise<ActionResult> {
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const tabsBefore = await chrome.tabs.query({}).catch(() => [] as chrome.tabs.Tab[]);
  const tabIdsBefore = new Set(tabsBefore.map(t => t.id).filter((id): id is number => typeof id === "number"));
  const agent = await withAgent(tabId);
  const searchIntent = classifySearchTarget(action.target);
  const resolveClick = (list: Candidate[]) =>
    findTarget(action.target, list, "click", { searchIntent });
  let cands = await agent.list();
  let target = resolveClick(cands);

  if (!target && !action.target.name?.toLowerCase().includes("close")) {
    const dismissed = await dismissObstructiveModalIfPresent(tabId);
    if (dismissed) {
      await settleDom(tabId, 150, 400);
      cands = await agent.list();
      target = resolveClick(cands);
    }
  }

  // Re-list candidates after a brief settle before declaring target stale (dynamic elements / lazy render)
  if (!target) {
    await sleep(250);
    cands = await agent.list();
    target = resolveClick(cands);
  }

  if (!target && action.target.coords) {
    // Exactly one click: trusted CDP input, with a single in-page fallback.
    const p = action.target.coords;
    const clicked = await cdpClick(tabId, p.x, p.y);
    const res = clicked
      ? { ok: true, targetId: undefined as string | undefined, error: undefined as string | undefined }
      : await agent.clickCoords(p.x, p.y);
    if (!res.ok) {
      return err("STALE_TARGET", res.error ?? "coordinate click failed", true);
    }
    const obsTab = await settleAfterAction(tabId, before, action.timeoutMs, tabIdsBefore);
    if (action.sessionId && obsTab !== tabId) {
      extensionSessions.get(action.sessionId)?.childTabIds.add(obsTab);
    }
    const after = await currentUrl(obsTab);
    void taskId;
    const isNewTab = obsTab !== tabId;
    const navDetected = isNewTab || after !== before;
    const expectsNav = /navigat|load\s*page|open\s*(tab|page)/i.test(action.verify || "");
    if (expectsNav && !navDetected) {
      return err("NAVIGATION_FAILED", `coordinate click (${p.x}, ${p.y}) accepted, but expected navigation did not occur (URL remained ${before})`, true);
    }
    let evidence = `clicked coords (${p.x}, ${p.y})`;
    if (isNewTab) {
      evidence = `opened new tab ${obsTab}: navigated ${before} → ${after} (observed destination content)`;
    } else if (navDetected) {
      evidence = `navigated ${before} → ${after} (observed destination content)`;
    } else {
      evidence = `clicked coords (${p.x}, ${p.y}) (click accepted; no navigation detected; url: ${after})`;
    }
    return {
      ok: true,
      targetId: res.targetId,
      observation: await observe(obsTab),
      evidence,
    };
  }

  if (!target) {
    return err("STALE_TARGET", `no matching element for ${JSON.stringify(action.target)}`, true);
  }

  const targetId = target.targetId;
  const targetName = target.name || action.target.name || "";
  const dispatchedAt = Date.now();

  // 1. Activation point: a chat row's true centre when the planner matched an
  //    inner label, so list selection handlers fire reliably.
  const coords = await agent.clickPoint(targetId, targetName);
  let cdpSuccess = false;
  if (coords && coords.x > 0 && coords.y > 0) {
    // 2. One trusted click via Chrome DevTools Protocol
    cdpSuccess = await cdpClick(tabId, coords.x, coords.y);
  }

  // 3. In-page click only if CDP was unavailable — never both.
  if (!cdpSuccess) {
    const res = await agent.click(targetId, targetName);
    if (!res.ok) {
      return err("STALE_TARGET", res.error ?? `no matching element for ${JSON.stringify(action.target)}`, true);
    }
  }
  let obsTab = await settleAfterAction(tabId, before, action.timeoutMs, tabIdsBefore);
  if (action.sessionId && obsTab !== tabId) {
    extensionSessions.get(action.sessionId)?.childTabIds.add(obsTab);
  }
  let after = await currentUrl(obsTab);
  void taskId;
  let isNewTab = obsTab !== tabId;
  let navDetected = isNewTab || after !== before;

  // Fallback for link clicks where browser popup blocker suppresses synthetic target="_blank":
  if (!navDetected && target.href && /^https?:\/\//.test(target.href)) {
    try {
      const created = await chrome.tabs.create({ url: target.href, active: true, openerTabId: tabId });
      if (created.id !== undefined) {
        await waitForStable(created.id, action.timeoutMs);
        obsTab = created.id;
        after = await currentUrl(obsTab);
        isNewTab = true;
        navDetected = true;
        if (action.sessionId) {
          const sess = extensionSessions.get(action.sessionId);
          if (sess) {
            sess.childTabIds.add(obsTab);
            sess.activeTabId = obsTab;
          }
        }
      }
    } catch {}
  }

  const expectsNav = /navigat|load\s*page|open\s*(tab|page)/i.test(action.verify || "");
  if (expectsNav && !navDetected) {
    return err("NAVIGATION_FAILED", `click accepted on ${targetName || targetId}, but expected navigation did not occur (URL remained ${before})`, true);
  }
  let evidence = `clicked ${targetName || targetId}`;
  if (isNewTab) {
    evidence = `opened new tab ${obsTab}: navigated ${before} → ${after} (observed destination content)`;
  } else if (navDetected) {
    evidence = `navigated ${before} → ${after} (observed destination content)`;
  } else {
    evidence = `clicked ${targetName || targetId} (click accepted; no navigation detected; url: ${after})`;
  }
  return {
    ok: true,
    targetId: targetId,
    observation: await observe(obsTab),
    evidence,
    verification: {
      targetConfidence: action.target.targetId === targetId ? .95 : .85,
      dispatchAcknowledged: cdpSuccess,
      surfaceChanged: navDetected || undefined,
      transportStatus: await networkStatusSince(tabId, dispatchedAt),
    },
  };
}

function findSubmitControl(cands: AgentElement[]): AgentElement | undefined {
  return cands.find(c => {
    if (!c.enabled || !c.visible) return false;
    const buttonLike = c.tag === "button"
      || c.role === "button"
      || c.role === "submit"
      || c.inputType === "submit"
      || c.inputType === "image";
    // Never mistake the search textbox itself for the submit control.
    if (!buttonLike) return false;
    const n = c.name.toLowerCase();
    if (n.includes("voice") || n.includes("ptt") || n.includes("microphone") || n.includes("record")) return false;
    return n === "send"
      || n.startsWith("send ")
      || n.includes("send message")
      || n === "search"
      || n.startsWith("search ")
      || n.includes("search for")
      || n.includes("submit")
      || c.inputType === "submit";
  });
}

/** Did a search submission change navigation or the application's page state? */
export function searchTransitionChanged(
  before: PageStateProbe | null,
  after: PageStateProbe | null,
): boolean {
  if (!before || !after) return false;
  if (before.url !== after.url || before.title !== after.title) return true;
  if (before.resultCount !== after.resultCount) return true;
  return before.textHash !== after.textHash || before.textLength !== after.textLength;
}

/**
 * Give a search submit time to take effect. Real navigations land immediately,
 * but SPAs (Flipkart) may route several seconds after the form event. Poll URL
 * and page state instead of failing on the first quiet sample.
 */
async function waitForSearchOutcome(
  tabId: number,
  beforeUrl: string,
  baseline: PageStateProbe | null,
  budgetMs: number,
  probe: () => Promise<PageStateProbe | null>,
): Promise<{ url: string; state: PageStateProbe | null }> {
  const deadline = Date.now() + Math.max(800, budgetMs);
  let state = baseline;
  let nextProbeAt = Date.now();
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    const url = tab?.url ?? "";
    if (url && url !== beforeUrl) return { url, state };
    if (tab?.status === "loading") {
      const settledUrl = await waitForStable(tabId, Math.min(budgetMs, 4_000));
      return { url: settledUrl || url, state };
    }
    if (Date.now() >= nextProbeAt) {
      nextProbeAt = Date.now() + 750;
      const next = await probe();
      if (next) {
        state = next;
        if (searchTransitionChanged(baseline, next)) {
          return { url: url || await currentUrl(tabId), state: next };
        }
      }
    }
    await sleep(150);
  }
  return { url: await currentUrl(tabId).catch(() => beforeUrl), state };
}

async function doType(action: Extract<BrowserAction, { type: "type" }>, taskId: string, tabId: number): Promise<ActionResult> {
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const tabsBefore = await chrome.tabs.query({}).catch(() => [] as chrome.tabs.Tab[]);
  const tabIdsBefore = new Set(tabsBefore.map(t => t.id).filter((id): id is number => typeof id === "number"));
  const agent = await withAgent(tabId);
  let searchIntent = classifySearchTarget(action.target);
  const resolveType = (list: Candidate[]) =>
    findTarget(action.target, list, "type", { searchIntent });
  let cands = await agent.list();
  let target = resolveType(cands);
  if (!target) {
    const dismissed = await dismissObstructiveModalIfPresent(tabId);
    if (dismissed) {
      await settleDom(tabId, 150, 400);
      cands = await agent.list();
      target = resolveType(cands);
    }
  }
  // Re-list candidates after a brief settle before declaring target stale
  if (!target) {
    await sleep(250);
    cands = await agent.list();
    target = resolveType(cands);
  }
  if (!target) {
    return err("STALE_TARGET", `no safely identifiable element for ${JSON.stringify(action.target)}`, true);
  }
  const targetId = target.targetId;
  const targetName = target.name || action.target.name || "";
  const typedText = action.params.text;
  const clear = action.params.clear ?? true;
  const submitRequested = Boolean(action.params.submit);
  searchIntent = searchIntent || classifySearchTarget(action.target, target);

  // If a modal popped up after listing, dismiss it so input field is reachable
  if (!action.target.name?.toLowerCase().includes("close")) {
    const dismissed = await dismissObstructiveModalIfPresent(tabId);
    if (dismissed) {
      await settleDom(tabId, 150, 300);
    }
  }

  const findSubmitPoint = async (): Promise<FocusPoint | null> => {
    // Poll for the Send/Search button instead of sleeping a fixed 150ms: exit
    // as soon as React/DOM exposes it, capped so a missing control still falls
    // back to Enter.
    const renderDeadline = Date.now() + 400;
    let sendBtn = findSubmitControl(await agent.list());
    while (!sendBtn && Date.now() < renderDeadline) {
      await sleep(40);
      sendBtn = findSubmitControl(await agent.list());
    }
    if (!sendBtn) return null;
    const sendCoords = await agent.getCoords(sendBtn.targetId, sendBtn.name);
    return sendCoords && sendCoords.x > 0 && sendCoords.y > 0
      ? { x: sendCoords.x, y: sendCoords.y }
      : null;
  };

  const refocus = async (): Promise<FocusPoint | null> => {
    const fresh = await agent.getCoords(targetId, targetName);
    return fresh && fresh.x > 0 && fresh.y > 0 ? { x: fresh.x, y: fresh.y } : null;
  };

  let baselineState: PageStateProbe | null = null;
  const captureBaseline = async (): Promise<PageStateProbe | null> => {
    try {
      baselineState = await agent.pageState();
      return baselineState;
    } catch {
      return null;
    }
  };

  // 1. Type through Chrome's native input pipeline. When submit is requested,
  //    the same debugger session verifies the value, waits for autocomplete,
  //    re-focuses if the SPA rerendered, and submits — no detach/reattach gap.
  let typingMode = "native";
  const dispatchedAt = Date.now();
  let verified = false;
  let submitted = false;
  let nativeSubmitMethod: "button" | "enter" | undefined;
  let nativeError: string | undefined;

  if (target.tag !== "select") {
    const coords = await agent.getCoords(targetId, targetName);
    const native = await cdpType(tabId, coords, typedText, clear, {
      targetId,
      targetName,
      submit: submitRequested ? findSubmitPoint : undefined,
      refocus: submitRequested ? refocus : undefined,
      beforeSubmit: submitRequested ? captureBaseline : undefined,
    });
    verified = Boolean(native.typed && native.verified);
    submitted = native.submitted;
    nativeSubmitMethod = native.submitMethod;
    nativeError = native.error;
  }

  // 2. In-page fallback only when the native path is unavailable or could not
  //    verify the actual DOM value. It is not described as trusted input.
  if (!verified) {
    let res: { ok: boolean; error?: string; value?: string };
    try {
      res = await agent.type(targetId, typedText, clear, targetName);
    } catch (e) {
      const detail = String(e instanceof Error ? e.message : e).slice(0, 120);
      return err("STALE_TARGET", `type failed: ${detail}`, true);
    }
    if (!res.ok) return err("STALE_TARGET", res.error ?? "type failed", true);
    typingMode = target.tag === "select" ? "in-page select" : "in-page fallback";

    if (target.tag !== "select") {
      const probe = await agent.readValue(targetId, targetName);
      const actual = res.value ?? probe.value;
      if (!probe.ok && res.value === undefined) {
        return err("STALE_TARGET", "typed value could not be read back from the page", true);
      }
      if (!typedValueMatches(actual, typedText, clear)) {
        return err(
          "STALE_TARGET",
          `typed value not confirmed in the DOM (expected ${JSON.stringify(typedText.slice(0, 60))}, got ${JSON.stringify(String(actual ?? "").slice(0, 60))})`,
          true,
        );
      }
    }

    // Native submission was unavailable; try one CDP submit, then the
    // in-page form/Enter fallback.
    if (submitRequested) {
      await captureBaseline();
      const point = await findSubmitPoint();
      if (point) submitted = await cdpClick(tabId, point.x, point.y);
      if (!submitted) submitted = await cdpPressKey(tabId, "Enter");
      if (!submitted) {
        const fallback = await agent.submit(targetId, targetName);
        submitted = fallback.ok;
      }
    }
  }

  // Let a controlled input paint validation/autocomplete before this
  // observation is cached for the next planning step.
  await settleDom(tabId, 100, 300);

  let obsTab = tabId;
  if (submitRequested) {
    if (!submitted) {
      // CDP was entirely unavailable: one in-page submit is the honest fallback.
      await captureBaseline();
      const fallback = await agent.submit(targetId, targetName);
      submitted = fallback.ok;
    }
    if (submitted) {
      obsTab = await settleAfterAction(tabId, before, Math.min(action.timeoutMs, 2500), tabIdsBefore);
      if (action.sessionId && obsTab !== tabId) {
        const sess = extensionSessions.get(action.sessionId);
        if (sess) {
          sess.childTabIds.add(obsTab);
          sess.activeTabId = obsTab;
        }
      }
    }
  }

  const after = await currentUrl(obsTab);
  let navChanged = obsTab !== tabId || after !== before;
  let finalUrl = after;
  if (submitRequested && searchIntent && verified && !navChanged) {
    // Some SPAs route seconds after the submit event. Poll for a real outcome
    // before declaring the submission failed.
    const budget = Math.min(action.timeoutMs, 10_000);
    let outcome: { url: string; state: PageStateProbe | null } = { url: after, state: baselineState };
    try {
      outcome = await withDebuggerDeadline(
        waitForSearchOutcome(obsTab, before, baselineState, budget, () => agent.pageState().catch(() => null)),
        budget + 1_500,
        "search outcome",
      );
    } catch {
      // Keep the last URL read; a missing outcome still fails closed below.
    }
    const finalUrlFromOutcome = outcome.url || after;
    finalUrl = finalUrlFromOutcome;
    navChanged = obsTab !== tabId || finalUrlFromOutcome !== before;
    if (!navChanged && !searchTransitionChanged(baselineState, outcome.state)) {
      return err(
        "NAVIGATION_FAILED",
        `search submission on ${targetName || targetId} was sent, but no navigation or search-state change was observed (url remained ${before})`,
        true,
      );
    }
  }

  void taskId;
  const nativeNote = nativeError && typingMode !== "native"
    ? ` (native path unavailable: ${nativeError.slice(0, 120)})`
    : "";
  const submitEvidence = submitRequested
    ? (navChanged
      ? `submitted via ${nativeSubmitMethod ?? "fallback"} and navigated (${finalUrl})`
      : `submitted via ${nativeSubmitMethod ?? "fallback"}; search state changed`)
    : "";
  const finalObservation = await observe(obsTab);
  return {
    ok: true,
    targetId: targetId,
    observation: finalObservation,
    verification: {
      targetConfidence: action.target.targetId === targetId ? .95 : .85,
      dispatchAcknowledged: typingMode === "native" && (!submitRequested || Boolean(nativeSubmitMethod)),
      surfaceChanged: navChanged || (submitRequested && searchIntent ? true : undefined),
      transportStatus: await networkStatusSince(tabId, dispatchedAt),
    },
    evidence: submitRequested
      ? `${typingMode} typing verified${nativeNote}; ${submitEvidence}`
      : `${typingMode} typing verified${nativeNote}`,
  };
}

async function doPress(action: Extract<BrowserAction, { type: "press" }>, tabId: number): Promise<ActionResult> {
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const tabsBefore = await chrome.tabs.query({}).catch(() => [] as chrome.tabs.Tab[]);
  const tabIdsBefore = new Set(tabsBefore.map(t => t.id).filter((id): id is number => typeof id === "number"));
  const key = action.params.key;

  if (key === "Enter") {
    const trusted = await cdpPressKey(tabId, "Enter");
    if (!trusted) {
      const agent = await withAgent(tabId);
      const fallback = await agent.submit("", "");
      if (!fallback.ok) return err("UNKNOWN", fallback.error ?? "Enter dispatch failed", true);
    }
  } else {
    await execScript({
      target: { tabId },
      func: (k: string) => {
        const target = document.activeElement ?? document.body;
        const opts = { key: k, bubbles: true, cancelable: true };
        target.dispatchEvent(new KeyboardEvent("keydown", opts));
        target.dispatchEvent(new KeyboardEvent("keypress", opts));
        target.dispatchEvent(new KeyboardEvent("keyup", opts));
      },
      args: [key ?? ""],
    });
  }
  const obsTab = await settleAfterAction(tabId, before, action.timeoutMs, tabIdsBefore);
  if (action.sessionId && obsTab !== tabId) {
    const sess = extensionSessions.get(action.sessionId);
    if (sess) {
      sess.childTabIds.add(obsTab);
      sess.activeTabId = obsTab;
    }
  }
  return { ok: true, observation: await observe(obsTab), evidence: `pressed ${key}` };
}

async function doScroll(action: Extract<BrowserAction, { type: "scroll" }>, tabId: number): Promise<ActionResult> {
  await ensureTabActive(tabId);
  const agent = await withAgent(tabId);
  const res = await agent.scroll(action.params.direction, action.params.containerId);
  // Infinite/lazy lists render after the scroll event; give them one paint so
  // the cached observation actually contains the newly revealed items.
  if (res.ok) await settleDom(tabId, 80, 400);
  return {
    ok: res.ok,
    observation: await observe(tabId),
    evidence: res.ok ? `scrollY=${res.scrollY}` : "scroll target not found",
    error: res.ok ? undefined : { code: "STALE_TARGET", message: "scroll container not found", recoverable: true },
  };
}

async function doZoom(action: Extract<BrowserAction, { type: "zoom" }>, tabId: number): Promise<ActionResult> {
  const p = action.params;
  const current = await chrome.tabs.getZoom(tabId);
  let newZoom = current;
  if (p.direction === "in") newZoom = Math.min(current + 0.25, 3.0);
  else if (p.direction === "out") newZoom = Math.max(current - 0.25, 0.25);
  else if (p.direction === "reset") newZoom = 1.0;
  else if (p.factor !== undefined) newZoom = p.factor;
  await chrome.tabs.setZoom(tabId, newZoom);
  const after = await chrome.tabs.getZoom(tabId);
  return { ok: true, observation: await observe(tabId), evidence: `zoom=${after.toFixed(2)}` };
}

async function doNewTab(action: Extract<BrowserAction, { type: "new_tab" }>): Promise<ActionResult> {
  if (isBlockedUrl(action.params.url)) return err("NAVIGATION_FAILED", `blocked scheme: ${action.params.url}`, false);
  const tab = await chrome.tabs.create({ url: action.params.url, active: true });
  if (action.sessionId && tab.id !== undefined) {
    const sess = extensionSessions.get(action.sessionId);
    if (sess) {
      sess.childTabIds.add(tab.id);
      sess.activeTabId = tab.id;
    }
  }
  await waitForStable(tab.id!, action.timeoutMs, action.params.url);
  // The backend registers child tabs by parsing this evidence ("opened new
  // tab <id>") — keep the phrasing in sync with agentLoop's detector.
  return { ok: true, observation: await observe(tab.id!), evidence: `opened new tab ${tab.id} at ${action.params.url}` };
}

/** Wait until the navigation completes and the DOM is interactive and quiet. */
async function waitForStable(tabId: number, timeoutMs: number, expectedUrl?: string): Promise<string> {
  const start = Date.now();
  const budget = Math.max(200, Math.min(timeoutMs, 8000));
  const initial = await chrome.tabs.get(tabId).catch(() => null);
  const initialUrl = initial?.url ?? "";

  // 1. Wait for the navigation to actually begin. chrome.tabs.get can still
  // report the previous document as "complete" right after tabs.update().
  for (let i = 0; i < 12 && Date.now() - start < 600; i++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) break;
    if (tab.status === "loading") break;
    if (expectedUrl ? samePageUrl(tab.url ?? "", expectedUrl) : tab.url !== initialUrl) break;
    await sleep(50);
  }

  // 2. Wait for the network load to complete or DOM to become interactive.
  while (Date.now() - start < budget) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.status === "complete" && tab.url) break;
    if (Date.now() - start >= 400) {
      const status = await pageStatus(tabId);
      if (status && status.ready && status.interactiveCount >= 3) break;
    }
    await sleep(50);
  }

  // 3. Hydration: fast poll, exit as soon as the DOM is interactive and
  // stable. Low-element pages no longer pay a fixed 3.5s penalty.
  const hydrateDeadline = Math.min(Date.now() + 2000, start + budget);
  let previousInteractive = -1;
  let stableSamples = 0;
  while (Date.now() < hydrateDeadline) {
    const status = await pageStatus(tabId);
    if (status) {
      if (status.interactiveCount === previousInteractive) {
        stableSamples++;
      } else {
        previousInteractive = status.interactiveCount;
        stableSamples = 0;
      }
      if (isPageSettled(status, previousInteractive, stableSamples)) break;
    }
    await sleep(40);
  }

  const tab = await chrome.tabs.get(tabId).catch(() => null);
  return tab?.url ?? "";
}

async function captureScreenshot(tabId: number): Promise<string | undefined> {
  if (typeof chrome === "undefined" || !chrome.tabs?.captureVisibleTab) return undefined;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!tab.windowId) return undefined;
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 45 });
    return dataUrl;
  } catch {
    return undefined;
  }
}

/** Surfaces where a content script can never run (about:blank, chrome://, data:, ...). */
function isScriptableUrl(url: string): boolean {
  const u = (url || "").trim();
  if (!u) return false;
  return !isBlockedUrl(u);
}

/**
 * Metadata-only observation for a tab we cannot script yet.
 *
 * A freshly created tab starts on about:blank, and Chrome refuses script
 * injection there ("Extension manifest must request permission to access this
 * host"). Returning the tab's own URL with no elements lets the planner emit a
 * navigate instead of the mission dying on its first observation.
 */
function emptyObservation(tabId: number, url: string, title: string): BrowserObservation {
  return sanitizeObservation({
    tabId,
    url,
    title,
    interactive: [],
    scroll: { y: 0, viewport: { w: 1280, h: 800 } },
    dpr: 1,
  });
}

export async function observe(tabId: number, sessionId?: string): Promise<BrowserObservation> {
  await ensureTabActive(tabId);
  const tab = await chrome.tabs.get(tabId);
  const url = tab.url ?? "";
  const title = tab.title ?? "";

  if (!isScriptableUrl(url)) {
    return emptyObservation(tabId, url, title);
  }

  let list: AgentElement[] = [];
  let meta = { y: 0, w: 0, h: 0, dpr: 1 };
  let pageText: string | undefined;
  try {
    const agent = await withAgent(tabId);
    list = await agent.list();

    // If the list is empty, poll briefly for SPA elements before returning an
    // empty observation. 100ms polls replace the old 400ms cadence, so a page
    // that hydrates in 60ms is observed immediately instead of after 400ms.
    if (list.length === 0) {
      for (let i = 0; i < 10; i++) {
        await sleep(100);
        list = await agent.list();
        if (list.length > 0) break;
      }
    }

    const [m] = await execScript({
      target: { tabId },
      func: () => ({ y: scrollY, w: innerWidth, h: innerHeight, dpr: devicePixelRatio }),
    });
    meta = m?.result ?? meta;

    try {
      const txt = await agent.extractText(4000);
      if (txt && txt.trim().length > 0) {
        pageText = txt.trim();
      }
    } catch {}
  } catch {
    // Injection refused (host permission, PDF viewer, tab mid-commit): degrade
    // to metadata so the loop can recover instead of aborting the mission.
    return emptyObservation(tabId, url, title);
  }

  const screenshot = await captureScreenshot(tabId);

  let openTabs: TabInfo[] | undefined;
  try {
    const all = await chrome.tabs.query({});
    let allowedTabIds: Set<number> | null = null;
    if (sessionId && extensionSessions.has(sessionId)) {
      const sess = extensionSessions.get(sessionId)!;
      allowedTabIds = new Set([sess.primaryTabId, ...sess.childTabIds]);
    } else {
      for (const sess of extensionSessions.values()) {
        if (sess.primaryTabId === tabId || sess.childTabIds.has(tabId)) {
          allowedTabIds = new Set([sess.primaryTabId, ...sess.childTabIds]);
          break;
        }
      }
    }

    const filtered = allowedTabIds
      ? all.filter(t => t.id !== undefined && allowedTabIds!.has(t.id) && isDrivableTabUrl(t.url ?? ""))
      : all.filter(t => t.id === tabId);

    openTabs = filtered.map(t => ({
      tabId: t.id!,
      url: t.url ?? "",
      title: t.title ?? "",
      active: Boolean(t.id === tabId),
    }));
  } catch {}

  return sanitizeObservation({
    tabId,
    url,
    title,
    tabs: openTabs,
    pageText,
    interactive: list.map(e => ({
      targetId: e.targetId,
      role: e.role,
      name: e.name,
      tag: e.tag || "el",
      inputType: e.inputType,
      search: e.search,
      href: e.href,
      visible: e.visible,
      enabled: e.enabled,
      // Values are never read at observation time; the privacy layer is only
      // a second line of defense.
      value: undefined,
      bounds: e.bounds,
      semantic: e.semantic,
    })),
    scroll: { y: meta.y, viewport: { w: meta.w, h: meta.h } },
    dpr: meta.dpr,
    screenshot,
  });
}
