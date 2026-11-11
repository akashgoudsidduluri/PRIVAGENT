import type {
  ActionResult,
  AgentError,
  BrowserAction,
  BrowserObservation,
  TabInfo,
} from "./contracts";
import { validateAction } from "./contracts";
import { findTarget } from "./targeting";
import { sanitizeObservation } from "./observer";

/** Injected content-script API shape (see lib/content.ts). */
interface AgentElement {
  targetId: string;
  role: string;
  name: string;
  tag: string;
  inputType?: string;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
  href?: string;
}

interface AgentApi {
  list: () => Promise<AgentElement[]>;
  click: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  clickCoords: (x: number, y: number) => Promise<{ ok: boolean; targetId?: string; error?: string }>;
  getCoords: (targetId: string, name?: string) => Promise<{ x: number; y: number; width: number; height: number } | null>;
  clickPoint: (targetId: string, name?: string) => Promise<{ x: number; y: number; width: number; height: number } | null>;
  type: (targetId: string, text: string, clear?: boolean, name?: string) => Promise<{ ok: boolean; error?: string }>;
  submit: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  scroll: (d: "up" | "down" | "top" | "bottom", containerId?: string) => Promise<{ ok: boolean; scrollY: number }>;
  extractText: (maxLen?: number) => Promise<string>;
}

export async function withAgent(tabId: number): Promise<AgentApi> {
  // Reuse the content script when it is already injected. Re-injecting the
  // whole bundle on every list/click/observe added a measurable round trip to
  // each step; a cheap presence probe is enough, and `list()` re-registers
  // element ids after any SPA re-render.
  let injected = false;
  try {
    const [probe] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => Boolean((window as unknown as { __navigatorAgent?: unknown }).__navigatorAgent),
    });
    injected = Boolean(probe?.result);
  } catch {
    injected = false;
  }
  if (!injected) {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });
  }
  return {
    list: async () => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => (window as unknown as { __navigatorAgent?: { list: () => AgentElement[] } }).__navigatorAgent?.list() ?? [],
      });
      return (r?.result as AgentElement[]) ?? [];
    },
    getCoords: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { getCoords: (i: string, n?: string) => { x: number; y: number; width: number; height: number } | null } }).__navigatorAgent?.getCoords(id, nm) ?? null,
        args: [targetId ?? "", name ?? ""],
      });
      return (r?.result as { x: number; y: number; width: number; height: number } | null) ?? null;
    },
    clickPoint: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { getClickPoint: (i: string, n?: string) => { x: number; y: number; width: number; height: number } | null } }).__navigatorAgent?.getClickPoint(id, nm) ?? null,
        args: [targetId ?? "", name ?? ""],
      });
      return (r?.result as { x: number; y: number; width: number; height: number } | null) ?? null;
    },
    click: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
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
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (cx: number, cy: number) => (window as unknown as { __navigatorAgent?: { clickCoords: (px: number, py: number) => { ok: boolean; targetId?: string; error?: string } } }).__navigatorAgent?.clickCoords(cx, cy) ?? { ok: false, error: "no agent" },
        args: [x, y],
      });
      return (r?.result as { ok: boolean; targetId?: string; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    type: async (targetId: string, text: string, clear?: boolean, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, val: string, cl: boolean, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { type: (i: string, v: string, c: boolean, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.type(id, val, cl, nm) ?? { ok: false, error: "no agent" },
        args: [targetId ?? "", text ?? "", Boolean(clear ?? true), name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    submit: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { submit: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.submit(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId ?? "", name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    scroll: async (direction: "up" | "down" | "top" | "bottom", containerId?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (dir: string, cid?: string) => (window as unknown as { __navigatorAgent?: { scroll: (d: any, c?: string) => { ok: boolean; scrollY: number } } }).__navigatorAgent?.scroll(dir as any, cid) ?? { ok: false, scrollY: 0 },
        args: [direction ?? "down", containerId ?? ""],
      });
      return (r?.result as { ok: boolean; scrollY: number }) ?? { ok: false, scrollY: 0 };
    },
    extractText: async (maxLen = 3000) => {
      const limit = typeof maxLen === "number" && Number.isFinite(maxLen) ? Math.max(100, maxLen) : 3000;
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (lim: number) =>
          (window as unknown as { __navigatorAgent?: { extractText?: (m?: number) => string } }).__navigatorAgent?.extractText?.(lim) ?? "",
        args: [limit],
      });
      return (r?.result as string) ?? "";
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
    const [res] = await chrome.scripting.executeScript({
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
    });
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
    await chrome.scripting.executeScript({
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
    });
  } catch {
    // Navigation replaced the document mid-wait.
  }
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

async function dispatchKeySequence(
  target: { tabId: number },
  sequence: Array<Record<string, unknown>>,
): Promise<void> {
  for (const event of sequence) {
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", event);
  }
}

/**
 * Type into the focused element through Chrome's native input pipeline.
 *
 * This is the primary path for modern SPAs (React-controlled inputs,
 * Flipkart's container `<div>`, WhatsApp Web's Lexical contenteditable).
 * `Input.insertText` goes through the same editing command a real keystroke
 * uses, so it fires `beforeinput`/`input` and updates the framework's state —
 * unlike assigning `HTMLInputElement.prototype.value`, which those apps
 * reject or overwrite on their next render.
 *
 * Returns false when the native path is unavailable, so the caller can fall
 * back to the in-page implementation.
 */
async function cdpType(
  tabId: number,
  coords: { x: number; y: number } | null,
  text: string,
  clear: boolean,
): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  // Without coordinates we cannot focus the field with trusted input, and
  // insertText would land in whatever happens to be focused instead.
  if (!coords || coords.x <= 0 || coords.y <= 0) return false;

  await ensureTabActive(tabId);
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e: any) {
    if (!e?.message?.includes("already attached")) {
      return false;
    }
  }

  try {
    const x = Math.round(coords.x);
    const y = Math.round(coords.y);
    // A trusted click both focuses the field and places the caret, which is
    // what allows insertText to target the right editable.
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1, pointerType: "mouse",
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1, pointerType: "mouse",
    });

    if (clear) {
      await dispatchKeySequence(target, cdpKeySequence("selectAll", isMacPlatform()));
      await dispatchKeySequence(target, cdpKeySequence("backspace"));
    }

    await chrome.debugger.sendCommand(target, "Input.insertText", { text });
    return true;
  } catch {
    return false;
  } finally {
    try {
      await chrome.debugger.detach(target);
    } catch {}
  }
}

async function cdpClick(tabId: number, x: number, y: number): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e: any) {
    if (!e?.message?.includes("already attached")) {
      return false;
    }
  }

  let pressed = false;
  try {
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: Math.round(x),
      y: Math.round(y),
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
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
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
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
    try {
      await chrome.debugger.detach(target);
    } catch {}
  }
}

async function cdpPressKey(tabId: number, key: string): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e: any) {
    if (!e?.message?.includes("already attached")) {
      return false;
    }
  }

  let down = false;
  try {
    if (key !== "Enter") return false;
    const [keyDown, keyUp] = cdpKeySequence("enter");
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", keyDown);
    // keyDown may already submit the form; treat a failed keyUp as a
    // successful attempt so no second Enter is dispatched.
    down = true;
    await new Promise(r => setTimeout(r, 15));
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", keyUp);
    return true;
  } catch {
    return down;
  } finally {
    try {
      await chrome.debugger.detach(target);
    } catch {}
  }
}

/**
 * Detect visible modal/dialog overlays and dismiss them if an unambiguous
 * safe close control (e.g. '✕', aria-label="Close", button.close, button._2doB4z) is present.
 */
export async function dismissObstructiveModalIfPresent(tabId: number): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.scripting) return false;
  try {
    const [r] = await chrome.scripting.executeScript({
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
  let cands = await agent.list();
  let target = findTarget(action.target, cands, "click");

  if (!target && !action.target.name?.toLowerCase().includes("close")) {
    const dismissed = await dismissObstructiveModalIfPresent(tabId);
    if (dismissed) {
      await settleDom(tabId, 150, 400);
      cands = await agent.list();
      target = findTarget(action.target, cands, "click");
    }
  }

  // Re-list candidates after a brief settle before declaring target stale (dynamic elements / lazy render)
  if (!target) {
    await sleep(250);
    cands = await agent.list();
    target = findTarget(action.target, cands, "click");
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
  };
}

function findSubmitControl(cands: AgentElement[]): AgentElement | undefined {
  return cands.find(c => {
    if (!c.enabled || !c.visible) return false;
    const n = c.name.toLowerCase();
    if (n.includes("voice") || n.includes("ptt") || n.includes("microphone") || n.includes("record")) return false;
    return n === "send"
      || n.startsWith("send ")
      || n.includes("send message")
      || n === "search"
      || n.startsWith("search ")
      || n.includes("search for")
      || (c.role === "button" && (n.includes("submit") || n.includes("search") || (c.tag === "button" && c.inputType === "submit")));
  });
}

async function doType(action: Extract<BrowserAction, { type: "type" }>, taskId: string, tabId: number): Promise<ActionResult> {
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const tabsBefore = await chrome.tabs.query({}).catch(() => [] as chrome.tabs.Tab[]);
  const tabIdsBefore = new Set(tabsBefore.map(t => t.id).filter((id): id is number => typeof id === "number"));
  const agent = await withAgent(tabId);
  let cands = await agent.list();
  let target = findTarget(action.target, cands, "type");
  if (!target) {
    const dismissed = await dismissObstructiveModalIfPresent(tabId);
    if (dismissed) {
      await settleDom(tabId, 150, 400);
      cands = await agent.list();
      target = findTarget(action.target, cands, "type");
    }
  }
  // Re-list candidates after a brief settle before declaring target stale
  if (!target) {
    await sleep(250);
    cands = await agent.list();
    target = findTarget(action.target, cands, "type");
  }
  if (!target) {
    return err("STALE_TARGET", `no matching element for ${JSON.stringify(action.target)}`, true);
  }
  const targetId = target.targetId;
  const targetName = target.name || action.target.name || "";

  // If a modal popped up after listing, dismiss it so input field is reachable
  if (!action.target.name?.toLowerCase().includes("close")) {
    const dismissed = await dismissObstructiveModalIfPresent(tabId);
    if (dismissed) {
      await settleDom(tabId, 150, 300);
    }
  }

  // 1. Type through Chrome's native input pipeline. A trusted click focuses
  //    the field and places the caret in the same session.
  const coords = await agent.getCoords(targetId, targetName);
  const clear = action.params.clear ?? true;
  const nativeTyped = await cdpType(tabId, coords, action.params.text, clear);

  // 2. In-page fallback only when the native path is unavailable. It is
  //    wrapped because a framework-owned node can throw from inside the
  //    injected script (the old "execution failed" path).
  if (!nativeTyped) {
    let res: { ok: boolean; error?: string };
    try {
      res = await agent.type(targetId, action.params.text, clear, targetName);
    } catch (e) {
      const detail = String(e instanceof Error ? e.message : e).slice(0, 120);
      return err("STALE_TARGET", `type failed: ${detail}`, true);
    }
    if (!res.ok) return err("STALE_TARGET", res.error ?? "type failed", true);
  }

  // Let a controlled input paint validation/autocomplete before this
  // observation is cached for the next planning step.
  await settleDom(tabId, 100, 300);

  let submitted = false;
  let obsTab = tabId;
  if (action.params.submit) {
    // Poll for the Send button instead of sleeping a fixed 150ms: exit as soon
    // as React/DOM exposes it, capped so a missing control still falls back.
    const renderDeadline = Date.now() + 400;
    let sendBtn = findSubmitControl(await agent.list());
    while (!sendBtn && Date.now() < renderDeadline) {
      await sleep(40);
      sendBtn = findSubmitControl(await agent.list());
    }
    if (sendBtn) {
      const sendCoords = await agent.getCoords(sendBtn.targetId, sendBtn.name);
      if (sendCoords && sendCoords.x > 0 && sendCoords.y > 0) {
        submitted = await cdpClick(tabId, sendCoords.x, sendCoords.y);
      }
    }
    // No button (or CDP unavailable): press Enter exactly once.
    if (!submitted) {
      submitted = await cdpPressKey(tabId, "Enter");
    }
    // Settle after submission attempt
    obsTab = await settleAfterAction(tabId, before, Math.min(action.timeoutMs, 2000), tabIdsBefore);
    if (action.sessionId && obsTab !== tabId) {
      const sess = extensionSessions.get(action.sessionId);
      if (sess) {
        sess.childTabIds.add(obsTab);
        sess.activeTabId = obsTab;
      }
    }
    // In-page fallback: if page hasn't navigated, trigger form submission directly
    const interimUrl = await currentUrl(obsTab);
    if (interimUrl === before) {
      const fallback = await agent.submit(targetId, targetName);
      if (fallback.ok) {
        obsTab = await settleAfterAction(tabId, before, action.timeoutMs, tabIdsBefore);
        if (action.sessionId && obsTab !== tabId) {
          const sess = extensionSessions.get(action.sessionId);
          if (sess) {
            sess.childTabIds.add(obsTab);
            sess.activeTabId = obsTab;
          }
        }
      }
    }
  }
  const after = await currentUrl(obsTab);
  void taskId;
  return {
    ok: true,
    targetId: targetId,
    observation: await observe(obsTab),
    evidence: action.params.submit
      ? (after !== before ? `submitted (${after})` : "typed + submitted")
      : "typed",
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
    await chrome.scripting.executeScript({
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

    const [m] = await chrome.scripting.executeScript({
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
      href: e.href,
      visible: e.visible,
      enabled: e.enabled,
      // Values are never read at observation time; the privacy layer is only
      // a second line of defense.
      value: undefined,
      bounds: e.bounds,
    })),
    scroll: { y: meta.y, viewport: { w: meta.w, h: meta.h } },
    dpr: meta.dpr,
    screenshot,
  });
}
