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
}

interface AgentApi {
  list: () => Promise<AgentElement[]>;
  getArticleContent: () => Promise<string>;
  getTableRows: () => Promise<string[]>;
  getOverlayText: () => Promise<string>;
  click: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  clickCoords: (x: number, y: number) => Promise<{ ok: boolean; targetId?: string; error?: string }>;
  getCoords: (targetId: string, name?: string) => Promise<{ x: number; y: number; width: number; height: number } | null>;
  clickPoint: (targetId: string, name?: string) => Promise<{ x: number; y: number; width: number; height: number } | null>;
  hitTest: (targetId: string, name?: string, x?: number, y?: number) => Promise<{ ok: boolean; error?: string }>;
  type: (targetId: string, text: string, clear: boolean, name?: string) => Promise<{ ok: boolean; error?: string }>;
  readValue: (targetId: string, name?: string) => Promise<{ ok: boolean; value?: string; error?: string }>;
  submit: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  scroll: (d: "up" | "down" | "top" | "bottom", containerId?: string) => Promise<{ ok: boolean; scrollY: number }>;
  hover: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  selectDate: (dateStr: string, targetId?: string, name?: string) => Promise<{ ok: boolean; error?: string; value?: string }>;
  sliderAdjust: (targetId: string, name?: string, percent?: number, direction?: "left" | "right") => Promise<{ ok: boolean; error?: string; value?: number; code?: "STALE_TARGET" | "NO_EFFECT" }>;
}

async function withAgent(tabId: number): Promise<AgentApi> {
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
    getArticleContent: async () => {
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => (window as unknown as { __navigatorAgent?: { getArticleContent?: () => string } }).__navigatorAgent?.getArticleContent?.() ?? "",
        });
        return (r?.result as string) ?? "";
      } catch {
        return "";
      }
    },
    getTableRows: async () => {
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => (window as unknown as { __navigatorAgent?: { getTableRows?: () => string[] } }).__navigatorAgent?.getTableRows?.() ?? [],
        });
        return (r?.result as string[]) ?? [];
      } catch {
        return [];
      }
    },
    getOverlayText: async () => {
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => (window as unknown as { __navigatorAgent?: { getOverlayText?: () => string } }).__navigatorAgent?.getOverlayText?.() ?? "",
        });
        return (r?.result as string) ?? "";
      } catch {
        return "";
      }
    },
    getCoords: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { getCoords: (i: string, n?: string) => { x: number; y: number; width: number; height: number } | null } }).__navigatorAgent?.getCoords(id, nm) ?? null,
        args: [targetId, name ?? ""],
      });
      return (r?.result as { x: number; y: number; width: number; height: number } | null) ?? null;
    },
    clickPoint: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { getClickPoint: (i: string, n?: string) => { x: number; y: number; width: number; height: number } | null } }).__navigatorAgent?.getClickPoint(id, nm) ?? null,
        args: [targetId, name ?? ""],
      });
      return (r?.result as { x: number; y: number; width: number; height: number } | null) ?? null;
    },
    hitTest: async (targetId: string, name?: string, x?: number, y?: number) => {
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          func: (id: string, nm?: string, px?: number, py?: number) =>
            (window as unknown as { __navigatorAgent?: { hitTest?: (i: string, n?: string, x?: number, y?: number) => { ok: boolean; error?: string } } }).__navigatorAgent?.hitTest?.(id, nm, px, py) ?? { ok: false, error: "no agent" },
          args: [targetId, name ?? "", x, y],
        });
        return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
      } catch (e: any) {
        return { ok: false, error: String(e?.message || e) };
      }
    },
    click: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) => (window as unknown as { __navigatorAgent?: { click: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.click(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId, name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    clickCoords: async (x: number, y: number) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (cx: number, cy: number) => (window as unknown as { __navigatorAgent?: { clickCoords: (px: number, py: number) => { ok: boolean; targetId?: string; error?: string } } }).__navigatorAgent?.clickCoords(cx, cy) ?? { ok: false, error: "no agent" },
        args: [x, y],
      });
      return (r?.result as { ok: boolean; targetId?: string; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    type: async (targetId: string, text: string, clear: boolean, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, val: string, cl: boolean, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { type: (i: string, v: string, c: boolean, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.type(id, val, cl, nm) ?? { ok: false, error: "no agent" },
        args: [targetId, text, clear, name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    readValue: async (targetId: string, name?: string) => {
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          func: (id: string, nm?: string) =>
            (window as unknown as { __navigatorAgent?: { readValue?: (i: string, n?: string) => { ok: boolean; value?: string; error?: string } } }).__navigatorAgent?.readValue?.(id, nm) ?? { ok: false, error: "no agent" },
          args: [targetId, name ?? ""],
        });
        return (r?.result as { ok: boolean; value?: string; error?: string }) ?? { ok: false, error: "execution failed" };
      } catch (e: any) {
        return { ok: false, error: String(e?.message || e) };
      }
    },
    submit: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { submit: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.submit(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId, name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    scroll: async (direction: "up" | "down" | "top" | "bottom", containerId?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (dir: string, cid?: string) => (window as unknown as { __navigatorAgent?: { scroll: (d: any, c?: string) => { ok: boolean; scrollY: number } } }).__navigatorAgent?.scroll(dir as any, cid) ?? { ok: false, scrollY: 0 },
        args: [direction, containerId ?? ""],
      });
      return (r?.result as { ok: boolean; scrollY: number }) ?? { ok: false, scrollY: 0 };
    },
    hover: async (targetId: string, name?: string) => {
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          func: (id: string, nm?: string) =>
            (window as unknown as { __navigatorAgent?: { hover?: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.hover?.(id, nm) ?? { ok: false, error: "no agent" },
          args: [targetId, name ?? ""],
        });
        return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
      } catch (e: any) {
        return { ok: false, error: String(e?.message || e) };
      }
    },
    selectDate: async (dateStr: string, targetId?: string, name?: string) => {
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          func: async (ds: string, id?: string, nm?: string) =>
            (window as unknown as { __navigatorAgent?: { selectDate?: (d: string, i?: string, n?: string) => Promise<{ ok: boolean; error?: string; value?: string }> } }).__navigatorAgent?.selectDate?.(ds, id, nm) ?? { ok: false, error: "no agent" },
          args: [dateStr, targetId ?? "", name ?? ""],
        });
        return (r?.result as { ok: boolean; error?: string; value?: string }) ?? { ok: false, error: "execution failed" };
      } catch (e: any) {
        return { ok: false, error: String(e?.message || e) };
      }
    },
    sliderAdjust: async (targetId: string, name?: string, percent?: number, direction?: "left" | "right") => {
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          func: async (id: string, nm?: string, p?: number, d?: "left" | "right") =>
            (window as unknown as { __navigatorAgent?: { sliderAdjust?: (i: string, n?: string, pc?: number, dir?: "left" | "right") => Promise<{ ok: boolean; error?: string; value?: number; code?: "STALE_TARGET" | "NO_EFFECT" }> } }).__navigatorAgent?.sliderAdjust?.(id, nm, p, d) ?? { ok: false, error: "no agent" },
          args: [targetId, name ?? "", percent, direction],
        });
        return (r?.result as { ok: boolean; error?: string; value?: number; code?: "STALE_TARGET" | "NO_EFFECT" }) ?? { ok: false, error: "execution failed" };
      } catch (e: any) {
        return { ok: false, error: String(e?.message || e) };
      }
    },
  };
}

const CONSOLE_PORTS = new Set(["5173", "4173"]);
const DASHBOARD_MARKER = "data-navigator-dashboard";

function isLocalHost(url: string): boolean {
  try {
    const u = new URL(url);
    return u.hostname === "localhost" || u.hostname === "127.0.0.1" || u.hostname === "[::1]";
  } catch {
    return false;
  }
}

/**
 * Fast path for the conventional Vite ports. Port detection alone is not
 * enough: Vite may auto-select any free port, so the authoritative check is
 * the dashboard's own `data-navigator-dashboard` marker below.
 */
export function isConsoleUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return isLocalHost(url)
      && CONSOLE_PORTS.has(u.port || (u.protocol === "https:" ? "443" : "80"));
  } catch {
    return false;
  }
}

/**
 * Ask a page whether it is the NAVIGATOR dashboard. The marker is set by the
 * dashboard's own HTML, so this works on any port/host and does not block
 * unrelated localhost sites. Injection failures are treated as "not the
 * dashboard" because privileged/blocked URLs are already excluded elsewhere.
 */
export async function isDashboardTab(tabId: number, url?: string): Promise<boolean> {
  if (url !== undefined && isConsoleUrl(url)) return true;
  try {
    const [result] = await chrome.scripting?.executeScript({
      target: { tabId },
      func: (marker: string) => document.documentElement?.getAttribute(marker) === "1",
      args: [DASHBOARD_MARKER],
    }) ?? [];
    return result?.result === true;
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
  // Prefer the tab the user is actually looking at.
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (active && usable(active) && !(await isDashboardTab(active.id!, active.url))) {
    return active.id!;
  }
  // Otherwise fall back to the most recently used permitted tab.
  const all = await chrome.tabs.query({});
  const candidates = all
    .filter(usable)
    .sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0));
  for (const candidate of candidates) {
    if (!(await isDashboardTab(candidate.id!, candidate.url))) return candidate.id!;
  }
  return null;
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
  const created = await chrome.tabs.create({ url: defaultUrl, active: true });
  if (created.id === undefined) {
    throw new Error("could not create a browser tab");
  }
  if (defaultUrl !== "about:blank") {
    await waitForStable(created.id, 5000, defaultUrl);
  }
  return created.id;
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
  const quiet = Math.max(30, quietMs);
  const cap = Math.max(quiet, maxMs);
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

/**
 * Post-action settle used by click/type-submit/press. Watches for a URL change,
 * a loading tab, or a newly activated tab (target=_blank), otherwise waits for
 * DOM quiescence. Returns the tab whose observation the next step should use.
 * It never activates an element, so single-click/single-submit is unchanged.
 */
async function settleAfterAction(tabId: number, beforeUrl: string, timeoutMs: number): Promise<number> {
  const cap = Math.max(150, Math.min(timeoutMs, 1200));
  await settleDom(tabId, 120, cap);

  // A click may have opened a new tab and made it active. Observe that tab,
  // not the old one, so the returned observation matches the next action's
  // active tab (and stays safe to cache).
  const active = await activeTab();
  if (active !== null && active !== tabId) {
    await waitForStable(active, timeoutMs);
    return active;
  }

  // A navigation can start just after the click's DOM churn settles.
  for (let i = 0; i < 4; i++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.status === "loading" || (tab?.url && tab.url !== beforeUrl)) {
      await waitForStable(tabId, timeoutMs);
      return tabId;
    }
    await sleep(30);
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
  const v = validateAction(action);
  if (!v.ok) {
    return { ok: false, error: { code: "PLAN_INVALID", message: `invalid action: ${v.error}`, recoverable: false } };
  }
  try {
    switch (action.type) {
      case "navigate": return await doNavigate(action);
      case "click": return await doClick(action, taskId);
      case "type": return await doType(action, taskId);
      case "press": return await doPress(action);
      case "scroll": return await doScroll(action);
      case "zoom": return await doZoom(action);
      case "new_tab": return await doNewTab(action);
      case "hover": return await doHover(action, taskId);
      case "select_date": return await doSelectDate(action, taskId);
      case "slider_adjust": return await doSliderAdjust(action, taskId);
      case "select_tab": {
        const target = await chrome.tabs.get(action.params.tabId).catch(() => null);
        if (target === null) return err("STALE_TARGET", "target tab no longer exists", true);
        if (await isDashboardTab(action.params.tabId, target.url)) {
          return err("PLAN_INVALID", "refusing to target the NAVIGATOR dashboard tab", false);
        }
        await chrome.tabs.update(action.params.tabId, { active: true });
        await waitForStable(action.params.tabId, action.timeoutMs);
        return {
          ok: true,
          observation: await observe(action.params.tabId),
          evidence: `tab ${action.params.tabId} active`,
        };
      }
      case "close_tab": {
        const target = await chrome.tabs.get(action.params.tabId).catch(() => null);
        if (target !== null && await isDashboardTab(action.params.tabId, target.url)) {
          return err("PLAN_INVALID", "refusing to close the NAVIGATOR dashboard tab", false);
        }
        await chrome.tabs.remove(action.params.tabId);
        const nextTabId = await getOrCreateActiveTab();
        return {
          ok: true,
          observation: await observe(nextTabId),
          evidence: `tab ${action.params.tabId} closed`,
        };
      }
      case "wait": {
        const tabId = await activeTab();
        if (tabId !== null) await ensureTabActive(tabId);
        await new Promise(r => setTimeout(r, Math.min(action.timeoutMs, 10_000)));
        return {
          ok: true,
          observation: tabId !== null ? await observe(tabId) : undefined,
          evidence: "waited for page/app to settle"
        };
      }
      case "observe": {
        // Observation is the loop's entry point; a missing tab must not abort
        // the mission before the first plan.
        const tabId = await getOrCreateActiveTab();
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

/**
 * Compare a field's read-back value with the requested text. Whitespace is
 * collapsed so a framework editor's incidental newlines do not look like a
 * failed keystroke; a genuinely discarded value still fails.
 */
export function typeApplied(expected: string, actual: string | undefined, clear: boolean): boolean {
  const normalize = (value: string) => value.replace(/\r\n/g, "\n").replace(/\s+/g, " ").trim();
  const want = normalize(expected ?? "");
  const got = normalize(actual ?? "");
  if (!want) return got.length === 0;
  return clear ? got === want : got.includes(want);
}

async function doNavigate(action: Extract<BrowserAction, { type: "navigate" }>): Promise<ActionResult> {
  const p = action.params;
  const existing = await activeTab();

  // Browser-history navigation needs a tab that exists; there is nowhere to
  // go back to otherwise, so this stays a recoverable error.
  if (p.direction === "back" || p.direction === "forward" || p.direction === "reload") {
    if (existing === null) {
      return err("NAVIGATION_FAILED", "no tab to navigate", true);
    }
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

  const cur = existing !== null ? await chrome.tabs.get(existing).catch(() => null) : null;
  const reusesDashboard = cur !== null && await isDashboardTab(existing!, cur.url);
  let targetTabId: number;
  if (existing === null || reusesDashboard || (cur?.url && isConsoleUrl(cur.url))) {
    // No usable tab (or only the dashboard): open the destination directly
    // instead of failing the mission before it starts.
    const newTab = await chrome.tabs.create({ url: p.url, active: true });
    if (newTab.id === undefined) {
      return err("NAVIGATION_FAILED", "could not create a browser tab", false);
    }
    targetTabId = newTab.id;
  } else {
    targetTabId = existing;
    await chrome.tabs.update(existing, { url: p.url });
  }
  const url = await waitForStable(targetTabId, action.timeoutMs, p.url);
  return { ok: true, observation: await observe(targetTabId), evidence: url };
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

async function cdpClick(
  tabId: number,
  x: number,
  y: number,
  stillOnTarget?: () => Promise<boolean>,
): Promise<boolean> {
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
    // A mouseover handler can remove or replace the control. Re-resolve the
    // target after the move and refuse to press when the point no longer
    // belongs to it, instead of activating whatever moved underneath.
    if (stillOnTarget) {
      const ok = await stillOnTarget();
      if (!ok) return false;
    }
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

async function cdpMouseMove(tabId: number, x: number, y: number): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  // Only detach what we attached: an existing session may belong to DevTools
  // or another debugger, and tearing it down would disrupt the user.
  let attachedHere = false;
  try {
    await chrome.debugger.attach(target, "1.3");
    attachedHere = true;
  } catch (e: any) {
    if (!e?.message?.includes("already attached")) {
      return false;
    }
  }
  try {
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: Math.round(x),
      y: Math.round(y),
    });
    return true;
  } catch {
    return false;
  } finally {
    if (attachedHere) {
      try {
        await chrome.debugger.detach(target);
      } catch {}
    }
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

async function doClick(action: Extract<BrowserAction, { type: "click" }>, taskId: string): Promise<ActionResult> {
  const tabId = await getOrCreateActiveTab();
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  const target = findTarget(action.target, cands, "click");

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
    const obsTab = await settleAfterAction(tabId, before, action.timeoutMs);
    const after = await currentUrl(obsTab);
    void taskId;
    return {
      ok: true,
      targetId: res.targetId,
      observation: await observe(obsTab),
      evidence: after !== before ? `navigated ${before} → ${after}` : `clicked coords (${p.x}, ${p.y})`,
    };
  }

  const targetId = target?.targetId || action.target.targetId || "";
  const targetName = target?.name || action.target.name || "";

  // 1. Activation point: a chat row's true centre when the planner matched an
  //    inner label, so list selection handlers fire reliably.
  const coords = await agent.clickPoint(targetId, targetName);
  let cdpSuccess = false;
  if (coords && coords.x > 0 && coords.y > 0) {
    const point = coords;
    // 2. One trusted click via Chrome DevTools Protocol. The hit test runs
    //    after mouseMoved, so a hover handler that removes the control
    //    produces STALE_TARGET instead of a click on its replacement.
    cdpSuccess = await cdpClick(tabId, point.x, point.y, async () => {
      try {
        const hit = await agent.hitTest(targetId, targetName, point.x, point.y);
        return hit.ok;
      } catch {
        return false;
      }
    });
  }

  // 3. In-page click only if CDP was unavailable — never both.
  if (!cdpSuccess) {
    const res = await agent.click(targetId, targetName);
    if (!res.ok) {
      return err("STALE_TARGET", res.error ?? `no matching element for ${JSON.stringify(action.target)}`, true);
    }
  }
  const obsTab = await settleAfterAction(tabId, before, action.timeoutMs);
  const after = await currentUrl(obsTab);
  void taskId;
  return {
    ok: true,
    targetId: targetId,
    observation: await observe(obsTab),
    evidence: after !== before ? `navigated ${before} → ${after}` : `clicked ${targetName || targetId}`,
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

async function doType(action: Extract<BrowserAction, { type: "type" }>, taskId: string): Promise<ActionResult> {
  const tabId = await getOrCreateActiveTab();
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  const target = findTarget(action.target, cands, "type");
  const targetId = target?.targetId || action.target.targetId || "";
  const targetName = target?.name || action.target.name || "";

  // Native date inputs reject synthetic insertText: the browser discards the
  // keystrokes and leaves the value empty. Route to the dedicated primitive
  // that writes through the native setter and fires input/change.
  const targetInputType = cands.find(c => c.targetId === targetId)?.inputType
    ?? (target as { inputType?: string } | null)?.inputType;
  if (targetInputType === "date" && /^\d{4}-\d{2}-\d{2}$/.test(action.params.text)) {
    const res = await agent.selectDate(action.params.text, targetId, targetName);
    if (!res.ok) {
      return err("NO_EFFECT", res.error ?? "date value was not applied", true);
    }
    await settleDom(tabId, 80, 200);
    return {
      ok: true,
      targetId,
      observation: await observe(tabId),
      evidence: `selected date ${action.params.text}`,
    };
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

  // Verify locally. The native and fallback paths both report what they
  // attempted, not what the page kept; a read-only/date/time widget can
  // discard the value. The field value never leaves the browser here.
  const readBack = await agent.readValue(targetId, targetName);
  if (!readBack.ok || !typeApplied(action.params.text, readBack.value, clear)) {
    return err(
      "NO_EFFECT",
      `typed text was not applied to ${targetName || targetId}`,
      true,
    );
  }

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
    obsTab = await settleAfterAction(tabId, before, Math.min(action.timeoutMs, 2000));
    // In-page fallback: if page hasn't navigated, trigger form submission directly
    const interimUrl = await currentUrl(obsTab);
    if (interimUrl === before) {
      const fallback = await agent.submit(targetId, targetName);
      if (fallback.ok) {
        obsTab = await settleAfterAction(tabId, before, action.timeoutMs);
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

async function doPress(action: Extract<BrowserAction, { type: "press" }>): Promise<ActionResult> {
  const tabId = await getOrCreateActiveTab();
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
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
      args: [key],
    });
  }
  const obsTab = await settleAfterAction(tabId, before, action.timeoutMs);
  return { ok: true, observation: await observe(obsTab), evidence: `pressed ${key}` };
}

async function doScroll(action: Extract<BrowserAction, { type: "scroll" }>): Promise<ActionResult> {
  const tabId = await getOrCreateActiveTab();
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

async function doZoom(action: Extract<BrowserAction, { type: "zoom" }>): Promise<ActionResult> {
  const tabId = await getOrCreateActiveTab();
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
  await waitForStable(tab.id!, action.timeoutMs, action.params.url);
  return { ok: true, observation: await observe(tab.id!), evidence: `opened ${action.params.url}` };
}

/**
 * Count visible overlays a hover can plausibly reveal. Comparing the count
 * before and after means success rests on an observed effect, not merely on
 * a dispatched event. Returns null when the probe cannot run.
 */
async function hoverEffectFingerprint(tabId: number): Promise<number | null> {
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const selector = [
          "[role='tooltip']", "[role='menu']", "[role='dialog']", "[role='listbox']",
          "[role='tree']", "[aria-expanded='true']", "details[open]", "dialog[open]",
          "[data-state='open']", "[data-popper-placement]",
          ".tooltip", ".dropdown-menu", ".submenu", ".popover", ".menu",
        ].join(",");
        const isHidden = (el: HTMLElement): boolean => {
          let node: HTMLElement | null = el;
          while (node) {
            if (node.hasAttribute("hidden")) return true;
            const style = getComputedStyle(node);
            if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") {
              return true;
            }
            node = node.parentElement;
          }
          return false;
        };
        let count = 0;
        document.querySelectorAll(selector).forEach(el => {
          if (el instanceof HTMLElement && !isHidden(el)) count++;
        });
        return count;
      },
    });
    return typeof r?.result === "number" ? r.result : null;
  } catch {
    return null;
  }
}

async function doHover(action: Extract<BrowserAction, { type: "hover" }>, taskId: string): Promise<ActionResult> {
  const tabId = await getOrCreateActiveTab();
  await ensureTabActive(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  const target = findTarget(action.target, cands, "click");
  const targetId = target?.targetId || action.target.targetId || "";
  const targetName = target?.name || action.target.name || "";

  const beforeEffect = await hoverEffectFingerprint(tabId);
  let cdpSuccess = false;
  const coords = await agent.clickPoint(targetId, targetName);
  if (coords && coords.x > 0 && coords.y > 0) {
    cdpSuccess = await cdpMouseMove(tabId, coords.x, coords.y);
  }
  let usedFallback = false;
  if (!cdpSuccess) {
    usedFallback = true;
    const res = await agent.hover(targetId, targetName);
    if (!res.ok) {
      return err("STALE_TARGET", res.error || `could not hover ${targetName || targetId}`, true);
    }
  }
  await settleDom(tabId, 80, 400);
  const afterEffect = await hoverEffectFingerprint(tabId);
  const overlayText = await agent.getOverlayText();
  const observation = await observe(tabId);
  if (overlayText) observation.overlayText = overlayText;
  void taskId;

  // A visible overlay is the effect, whether this hover opened it or it was
  // already open (the second hover in a row previously reported NO_EFFECT
  // while the tooltip sat on screen with its text unread).
  const countGrew = beforeEffect !== null && afterEffect !== null && afterEffect > beforeEffect;
  const visibleOverlay = (afterEffect ?? 0) > 0 && Boolean(overlayText);
  const effectObserved = afterEffect === null || countGrew || visibleOverlay;

  if (!effectObserved) {
    return {
      ok: false,
      observation,
      error: {
        code: "NO_EFFECT",
        message: `hover produced no observable overlay change for ${targetName || targetId}`
          + (usedFallback ? " (synthetic events cannot trigger CSS :hover)" : ""),
        recoverable: true,
      },
    };
  }

  const effectNote = beforeEffect !== null && afterEffect !== null
    ? `, overlays ${beforeEffect}→${afterEffect}`
    : ", effect unverified";
  const textNote = overlayText ? `, overlay text ${overlayText.length} chars` : "";
  return {
    ok: true,
    observation,
    evidence: `hovered ${targetName || targetId}${usedFallback ? " via synthetic events" : " via CDP"}${effectNote}${textNote}`,
  };
}

async function doSelectDate(action: Extract<BrowserAction, { type: "select_date" }>, taskId: string): Promise<ActionResult> {
  const tabId = await getOrCreateActiveTab();
  await ensureTabActive(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  const target = action.target ? findTarget(action.target, cands, "click") : undefined;
  const targetId = target?.targetId || action.target?.targetId || "";
  const targetName = target?.name || action.target?.name || "";

  const beforeUrl = await currentUrl(tabId);
  const res = await agent.selectDate(action.params.date, targetId, targetName);
  if (!res.ok) {
    return err("STALE_TARGET", res.error || `could not select date ${action.params.date}`, true);
  }
  const obsTab = await settleAfterAction(tabId, beforeUrl, action.timeoutMs);
  void taskId;
  return {
    ok: true,
    observation: await observe(obsTab),
    evidence: `selected date ${res.value || action.params.date}`,
  };
}

async function doSliderAdjust(action: Extract<BrowserAction, { type: "slider_adjust" }>, taskId: string): Promise<ActionResult> {
  const tabId = await getOrCreateActiveTab();
  await ensureTabActive(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  const target = findTarget(action.target, cands, "click");
  const targetId = target?.targetId || action.target.targetId || "";
  const targetName = target?.name || action.target.name || "";

  const res = await agent.sliderAdjust(targetId, targetName, action.params?.percent, action.params?.direction);
  await settleDom(tabId, 100, 500);
  void taskId;
  return {
    ok: res.ok,
    observation: await observe(tabId),
    evidence: res.ok
      ? `adjusted slider ${targetName || targetId}${typeof res.value === "number" ? ` to ${res.value}` : ""}`
      : "slider adjustment failed",
    error: res.ok
      ? undefined
      : {
        code: res.code ?? "STALE_TARGET",
        message: res.error || "slider failed",
        recoverable: true,
      },
  };
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

  // 2. Wait for the network load to complete.
  while (Date.now() - start < budget) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.status === "complete" && tab.url) break;
    await sleep(50);
  }

  // 3. Hydration: fast poll, exit as soon as the DOM is interactive and
  // stable. Low-element pages no longer pay a fixed 3.5s penalty.
  const hydrateDeadline = Math.min(Date.now() + 3500, start + budget);
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

export async function observe(tabId: number): Promise<BrowserObservation> {
  await ensureTabActive(tabId);
  const tab = await chrome.tabs.get(tabId);
  const url = tab.url ?? "";
  const title = tab.title ?? "";

  if (!isScriptableUrl(url)) {
    return emptyObservation(tabId, url, title);
  }

  let list: AgentElement[] = [];
  let articleContent = "";
  let tableRows: string[] = [];
  let meta = { y: 0, w: 0, h: 0, dpr: 1 };
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

    articleContent = await agent.getArticleContent().catch(() => "");
    tableRows = await agent.getTableRows().catch(() => []);

    const [m] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({ y: scrollY, w: innerWidth, h: innerHeight, dpr: devicePixelRatio }),
    });
    meta = m?.result ?? meta;
  } catch {
    // Injection refused (host permission, PDF viewer, tab mid-commit): degrade
    // to metadata so the loop can recover instead of aborting the mission.
    return emptyObservation(tabId, url, title);
  }

  const screenshot = await captureScreenshot(tabId);

  let openTabs: TabInfo[] | undefined;
  try {
    const all = await chrome.tabs.query({});
    const visible: TabInfo[] = [];
    for (const t of all) {
      if (t.id === undefined || !isDrivableTabUrl(t.url ?? "")) continue;
      // Only local tabs can be a dev-server dashboard; skip marker injection
      // into unrelated remote pages during every observation.
      if (isLocalHost(t.url ?? "") && await isDashboardTab(t.id, t.url)) continue;
      visible.push({
        tabId: t.id,
        url: t.url ?? "",
        title: t.title ?? "",
        active: Boolean(t.id === tabId || t.active),
      });
    }
    openTabs = visible;
  } catch {}

  return sanitizeObservation({
    tabId,
    url,
    title,
    tabs: openTabs,
    interactive: list.map(e => ({
      targetId: e.targetId,
      role: e.role,
      name: e.name,
      tag: e.tag || "el",
      inputType: e.inputType,
      visible: e.visible,
      enabled: e.enabled,
      // Values are never read at observation time; the privacy layer is only
      // a second line of defense.
      value: undefined,
      bounds: e.bounds,
    })),
    articleContent: articleContent || undefined,
    tableRows: tableRows && tableRows.length > 0 ? tableRows : undefined,
    scroll: { y: meta.y, viewport: { w: meta.w, h: meta.h } },
    dpr: meta.dpr,
    screenshot,
  });
}
