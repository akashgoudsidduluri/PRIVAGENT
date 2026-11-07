/**
 * Content script injected programmatically by the service worker.
 * Performs in-page interaction and returns serializable results.
 * Exposes window.__navigatorAgent for executeScript probes.
 */
export {}

interface AgentElement {
  targetId: string;
  role: string;
  name: string;
  tag: string;
  inputType?: string;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds: { x: number; y: number; width: number; height: number };
}

interface NavigatorAgent {
  list: () => AgentElement[];
  getArticleContent?: () => string;
  getTableRows?: () => string[];
  click: (targetId: string, name?: string) => { ok: boolean; error?: string };
  clickCoords: (x: number, y: number) => { ok: boolean; targetId?: string; error?: string };
  getCoords: (targetId: string, name?: string) => { x: number; y: number; width: number; height: number } | null;
  getClickPoint: (targetId: string, name?: string) => { x: number; y: number; width: number; height: number } | null;
  hitTest: (targetId: string, name?: string, x?: number, y?: number) => { ok: boolean; error?: string };
  getOverlayText: () => string;
  readValue: (targetId: string, name?: string) => { ok: boolean; value?: string; error?: string };
  type: (targetId: string, text: string, clear: boolean, name?: string) => { ok: boolean; error?: string };
  submit: (targetId: string, name?: string) => { ok: boolean; error?: string };
  scroll: (direction: "up" | "down" | "top" | "bottom", containerId?: string) => { ok: boolean; scrollY: number };
  hover?: (targetId: string, name?: string) => { ok: boolean; error?: string };
  selectDate?: (dateStr: string, targetId?: string, name?: string) => Promise<{ ok: boolean; error?: string; value?: string }>;
  sliderAdjust?: (targetId: string, name?: string, percent?: number, direction?: "left" | "right") => Promise<{ ok: boolean; error?: string; value?: number; code?: "STALE_TARGET" | "NO_EFFECT" }>;
}

declare global {
  interface Window { __navigatorAgent?: NavigatorAgent }
}

const registry = new Map<string, HTMLElement>();

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/** True when this element (or an ancestor) is hidden for text extraction. */
function isHiddenForExtraction(el: Element | null, stop?: Element | null): boolean {
  let node: Element | null = el;
  while (node) {
    if (node instanceof HTMLElement) {
      if (node.hasAttribute("hidden")) return true;
      const style = getComputedStyle(node);
      // Do not use offsetParent: fixed-position and sticky elements
      // legitimately report null there while still being visible.
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") {
        return true;
      }
    }
    if (node === stop) break;
    node = node.parentElement;
  }
  return false;
}

const NON_TEXT_SELECTOR = "script, style, noscript, template, svg, canvas";

// Navigation, chrome, ads and consent banners are never article body text.
const ARTICLE_EXCLUDE_SELECTOR = [
  "nav", "footer", "header", "aside",
  ".nav", ".menu", "#toc", ".toc", ".vector-toc", ".mw-editsection",
  "[role='navigation']", "[role='banner']", "[role='contentinfo']", "[role='complementary']",
  "[aria-hidden='true']",
  "#sidebar", ".sidebar", ".ad", ".ads", ".advert", ".advertisement",
  "[id*='cookie' i]", "[class*='cookie' i]", "[aria-label*='cookie' i]",
].join(", ");

/** Text of an element with hidden subtrees and nested tables removed. */
function visibleText(root: Element, skipNestedTables = false): string {
  let out = "";
  const walk = (node: Node) => {
    if (node.nodeType === 3) {
      out += node.nodeValue || "";
      return;
    }
    if (!(node instanceof Element)) return;
    if (node !== root) {
      if (node.matches(NON_TEXT_SELECTOR)) return;
      if (skipNestedTables && (node.tagName === "TABLE" || node.getAttribute("role") === "table")) return;
      if (isHiddenForExtraction(node)) return;
    }
    for (const child of Array.from(node.childNodes)) walk(child);
    out += " ";
  };
  walk(root);
  return out.replace(/\s+/g, " ").trim();
}


function cleanId(id: string | undefined): string {
  return (id ?? "").replace(/^#/, "").trim();
}

function queryAllWithShadow(root: Document | ShadowRoot | Element, selector: string): Element[] {
  const list: Element[] = Array.from(root.querySelectorAll(selector));
  try {
    const all = Array.from(root.querySelectorAll("*"));
    for (const el of all) {
      if (el.shadowRoot) {
        list.push(...queryAllWithShadow(el.shadowRoot, selector));
      }
    }
  } catch {}
  return list;
}

function candidates(): AgentElement[] {
  const selectors = [
    'button',
    'input',
    'textarea',
    'select',
    'a[href]',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[role="searchbox"]',
    '[role="menuitem"]',
    '[role="tab"]',
    '[role="option"]',
    '[role="row"]',
    '[role="gridcell"]',
    '[role="listitem"]',
    '[tabindex]',
    '[contenteditable]',
    '[contenteditable="true"]',
    '[data-testid]',
    '[data-tab]',
    'ytd-thumbnail',
    '#video-title',
    '#thumbnail',
    '.ytp-play-button',
    '.ytp-button',
    '[data-icon="send"]',
    '[data-testid="compose-btn-send"]',
    '.ytp-ad-skip-button',
    '.ytp-ad-skip-button-modern',
    '.ytp-skip-ad-button',
    '.ytp-ad-overlay-close-button',
    '[id^="skip-button"]',
    'button[aria-label*="close" i]',
    'button[aria-label*="skip" i]',
    'button.close',
    '[data-testid*="close"]',
    '#onetrust-accept-btn-handler',
    'button[id*="cookie"]',
    'button[id*="accept"]',
  ].join(', ');

  const nodes = queryAllWithShadow(document, selectors);
  const list: AgentElement[] = [];

  nodes.forEach((n, i) => {
    const el = n as HTMLElement;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const isHidden = style.display === "none" || style.visibility === "hidden" || style.opacity === "0";
    const hasSize = r.width > 0 && r.height > 0;
    if (isHidden || !hasSize) return;

    const input = el as HTMLInputElement;
    const rawId = el.id ? el.id.trim() : "";
    const targetId = rawId || `nav-el-${i}`;

    el.setAttribute("data-navigator-id", targetId);
    registry.set(targetId, el);
    if (rawId) {
      registry.set(`nav-el-${i}`, el);
    }

    const isAdSkip = el.matches('.ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button, [id^="skip-button"]');
    const isOverlayClose = el.matches('.ytp-ad-overlay-close-button, button[aria-label*="close" i], button.close, [data-testid*="close"]');
    const isVoiceBtn = el.matches('[data-icon="ptt"], [aria-label*="voice" i], [aria-label*="audio" i], [aria-label*="record" i]');
    const isSendBtn = !isVoiceBtn && el.matches('[data-testid="compose-btn-send"], [data-icon="send"], span[data-icon="send"], button[aria-label="Send" i], button[aria-label*="Send message" i]');
    const isComposer = el.matches('footer [contenteditable="true"], [data-tab="10"], [data-testid="conversation-compose-box-input"], footer [role="textbox"]');
    const isCancelVoice = el.matches('[data-icon="cancel"], [data-testid="btn-cancel"], [data-icon="delete-clip"], button[aria-label*="cancel" i]');

    const isWa = window.location.hostname.includes("whatsapp.com");
    const isWaGroupFilter = isWa && el.matches('button, [role="button"]') && el.textContent?.trim() === "Groups";
    const isWaCancelSearch = isWa && el.matches('[data-testid="chat-list-search-esc-button"], [data-icon="x-alt"], button[aria-label*="cancel search" i]');
    const isWaSearch = isWa && el.matches('div[data-tab="3"], [data-testid="chat-list-search"], [aria-label*="search" i]');
    const isWaChatRow = isWa && el.matches('div[role="listitem"], div[role="row"], div[data-testid*="chat-list-item"], div[data-testid="cell-frame-container"]');
    let waChatTitle = "";
    if (isWaChatRow) {
      const titleEl = el.querySelector('[data-testid="cell-frame-title"], span[title], div[title]');
      waChatTitle = titleEl?.getAttribute("title") || titleEl?.textContent?.trim() || "";
    }

    let name = (
      (isAdSkip ? "Skip Ad " : "") +
      (isOverlayClose && !el.textContent?.trim() ? "Close " : "") +
      (el.getAttribute("aria-label") ||
      el.getAttribute("title") ||
      input.placeholder ||
      (el.getAttribute("data-icon") ? `icon ${el.getAttribute("data-icon")}` : "") ||
      (el.getAttribute("data-testid") && !el.textContent?.trim() ? el.getAttribute("data-testid") : "") ||
      el.textContent ||
      input.value ||
      "")
    ).replace(/\s+/g, " ").trim().slice(0, 120);

    if (isAdSkip && !name) name = "Skip Ad";
    if (isOverlayClose && !name) name = "Close";
    if (isVoiceBtn) name = "Voice message";
    if (isCancelVoice) name = "Cancel Recording";
    if (isSendBtn) name = "Send";
    if (isComposer && (!name || name.length > 50)) name = "Type a message";
    if (isWaGroupFilter) name = "Groups";
    if (isWaCancelSearch) name = "Cancel search";
    if (isWaSearch && !name) name = "Search or start new chat";
    if (isWaChatRow && waChatTitle) name = waChatTitle;

    // Context-aware enrichment for product cards & pricing. A price is only
    // attributed when the container holds exactly one price and links to at
    // most one destination: shared grids would otherwise copy the first
    // product's price onto every card. Omitting context is better than lying.
    if (el.tagName === "A" || el.getAttribute("role") === "link" || el.getAttribute("role") === "button") {
      try {
        const card = el.closest('[data-component-type*="product"], [class*="product"], [class*="item"], [class*="card"], li') || el.parentElement;
        if (card) {
          const cardText = ((card as HTMLElement).innerText || card.textContent || "").replace(/\s+/g, " ");
          const prices = cardText.match(/(?:₹|Rs\.?|\$|EUR|€|£)\s*[\d,]+(?:\.\d+)?/gi) || [];
          const hrefs = new Set(
            Array.from(card.querySelectorAll("a[href]"))
              .map(a => a.getAttribute("href"))
              .filter((h): h is string => !!h),
          );
          if (prices.length === 1 && hrefs.size <= 1) {
            const price = prices[0].replace(/\s+/g, " ").trim();
            if (!name.includes(price)) name = `${name} [Price: ${price}]`.slice(0, 140);
          }
        }
      } catch {}
    }

    // Context-aware enrichment for inputs & textareas
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      try {
        let labelText = "";
        if (el.id) {
          const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (lbl) labelText = lbl.textContent?.trim() || "";
        }
        if (!labelText) {
          const parentLbl = el.closest("label");
          if (parentLbl) labelText = parentLbl.textContent?.trim() || "";
        }
        if (labelText && !name.includes(labelText)) {
          name = `${labelText}: ${name}`.slice(0, 140);
        }
      } catch {}
    }

    const role = isAdSkip ? "ad-skip" :
                 isOverlayClose ? "close-button" :
                 isComposer ? "textbox" :
                 isWaSearch ? "textbox" :
                 (isSendBtn || isVoiceBtn || isCancelVoice || isWaGroupFilter || isWaCancelSearch) ? "button" :
                 (isWaChatRow ? "listitem" : (el.getAttribute("role") || el.tagName.toLowerCase()));
    const cssSelector = rawId ? `#${CSS.escape(rawId)}` : `[data-navigator-id="${targetId}"]`;

    list.push({
      targetId,
      role,
      name,
      tag: el.tagName.toLowerCase(),
      inputType: el.tagName.toLowerCase() === "input" ? input.type : undefined,
      cssSelector,
      enabled: !(input as HTMLButtonElement).disabled && !el.hasAttribute("aria-disabled"),
      visible: true,
      bounds: {
        x: Math.round(r.left),
        y: Math.round(r.top),
        width: Math.round(r.width),
        height: Math.round(r.height),
      },
    });
  });

  return list;
}

function elementFor(targetId: string, name?: string): HTMLElement | null {
  const cid = cleanId(targetId);
  if (cid && registry.has(cid) && registry.get(cid)!.isConnected) {
    return registry.get(cid)!;
  }
  if (targetId && registry.has(targetId) && registry.get(targetId)!.isConnected) {
    return registry.get(targetId)!;
  }

  // 1. Direct query by data-navigator-id
  let el = cid ? (document.querySelector(`[data-navigator-id="${CSS.escape(cid)}"]`) as HTMLElement | null) : null;
  if (el && el.isConnected) return el;

  // 2. Direct query by ID or name attribute
  if (cid) {
    el = document.getElementById(cid);
    if (el && el.isConnected) return el;
    el = document.querySelector(`[name="${CSS.escape(cid)}"]`) as HTMLElement | null;
    if (el && el.isConnected) return el;
  }

  // 3. Search within open Shadow Roots
  if (cid) {
    const shadowNodes = queryAllWithShadow(document, `[data-navigator-id="${CSS.escape(cid)}"], #${CSS.escape(cid)}`);
    if (shadowNodes.length > 0 && (shadowNodes[0] as HTMLElement).isConnected) {
      return shadowNodes[0] as HTMLElement;
    }
  }

  // 4. Refresh candidates if registry was empty
  candidates();
  if (cid && registry.has(cid) && registry.get(cid)!.isConnected) {
    return registry.get(cid)!;
  }

  // 5. Fallback search by accessible name / textContent
  if (name && name.trim()) {
    const wanted = name.trim().toLowerCase();
    const all = queryAllWithShadow(document, 'button, input, textarea, select, a[href], [role="button"], [role="link"], [role="textbox"], [role="searchbox"], [role="row"], [role="listitem"], [role="gridcell"], [tabindex], [data-testid], span[title], div[title], ytd-thumbnail, #video-title') as HTMLElement[];

    // Specialized WhatsApp resolution:
    if (window.location.hostname.includes("whatsapp.com")) {
      if (wanted === "groups" || wanted === "group" || wanted.includes("group filter")) {
        const groupsBtn = all.find(e => {
          const t = (e.getAttribute("aria-label") || e.textContent || "").trim().toLowerCase();
          return t === "groups" || t === "group";
        });
        if (groupsBtn) return groupsBtn;
      }
      if (wanted.includes("first group") || wanted.includes("group chat") || wanted === "first chat") {
        const chatRow = document.querySelector('div[role="listitem"], div[role="row"], div[data-testid*="chat-list-item"], div[data-testid="cell-frame-container"]') as HTMLElement | null;
        if (chatRow) return chatRow;
      }
    }

    // Exact or substring match on aria-label or textContent or placeholder
    const matched = all.find(e => {
      const txt = (
        e.getAttribute("aria-label") ||
        e.getAttribute("title") ||
        e.textContent ||
        (e as HTMLInputElement).placeholder ||
        ""
      ).trim().toLowerCase();
      return txt === wanted || txt.includes(wanted) || (txt.length >= 3 && wanted.includes(txt));
    });
    if (matched) return matched;

    // Word token match
    const tokens = wanted.split(/\s+/).filter(t => t.length > 2);
    if (tokens.length > 0) {
      const tokenMatch = all.find(e => {
        const txt = (
          e.getAttribute("aria-label") ||
          e.getAttribute("title") ||
          e.textContent ||
          ""
        ).trim().toLowerCase();
        return tokens.every(tok => txt.includes(tok));
      });
      if (tokenMatch) return tokenMatch;
    }
  }

  return null;
}

/**
 * Chat rows / list items. WhatsApp renders a chat row as a container whose
 * inner text labels are what name-matching usually resolves, but the selection
 * handler lives on the row. Clicking the label's own centre can therefore hit
 * a non-activating child.
 */
const CHAT_ROW_SELECTOR =
  '[role="listitem"], [role="row"], div[tabindex="-1"], ' +
  '[data-testid*="cell-frame"], [data-testid*="list-item"], [data-testid*="chat-list-item"]';

const EXPLICIT_CONTROL_SELECTOR =
  'button, a[href], input, textarea, select, [role="button"], [role="link"], ' +
  '[role="checkbox"], [role="radio"], [contenteditable="true"]';

/** Nearest activatable row for a text label, or null for real controls. */
function rowFor(el: HTMLElement): HTMLElement | null {
  // A control keeps its own activation point: promoting it to the row would
  // click empty space instead of the button.
  if (el.matches(EXPLICIT_CONTROL_SELECTOR)) return null;
  const row = el.closest(CHAT_ROW_SELECTOR) as HTMLElement | null;
  if (!row || row === el) return null;
  const r = row.getBoundingClientRect();
  // Guard against `div[tabindex="-1"]` scroll containers: a real row is
  // small, not most of the viewport.
  if (r.height > Math.max(240, (window.innerHeight || 800) * 0.5)) return null;
  return row;
}

function extractArticleContent(): string {
  try {
    const mainEl = document.querySelector("#mw-content-text, article, main, [role='main'], .article-body, #content");
    const container = (mainEl as HTMLElement) || document.body;
    if (!container) return "";

    const blocks: string[] = [];
    const elements = Array.from(container.querySelectorAll("h1, h2, h3, h4, p, li"));
    for (const el of elements) {
      if (el.closest(ARTICLE_EXCLUDE_SELECTOR)) continue;
      if (isHiddenForExtraction(el)) continue;
      const tag = el.tagName.toLowerCase();
      const txt = visibleText(el);
      // Headings are often short ("History") but still carry the structure.
      const minLen = tag.startsWith("h") ? 2 : 15;
      if (!txt || txt.length < minLen) continue;
      if (tag.startsWith("h")) {
        blocks.push(`\n### ${txt}\n`);
      } else {
        blocks.push(txt);
      }
      if (blocks.join("\n").length >= 3500) break;
    }
    return blocks.join("\n").slice(0, 3500);
  } catch {
    return "";
  }
}

const MAX_TABLE_COLUMNS = 12;
const MAX_TABLE_CELL_LEN = 300;
const MAX_TABLE_DATA_ROWS = 25;
const MAX_TABLES = 10;

function cellText(cell: Element): string {
  // A parent cell must not absorb a nested table's rows: that duplicates data
  // and merges unrelated tables into one malformed row.
  return visibleText(cell, true).slice(0, MAX_TABLE_CELL_LEN);
}

function extractTableRows(): string[] {
  try {
    const out: string[] = [];
    const tables = Array.from(document.querySelectorAll("table"))
      .filter(t => !t.parentElement?.closest("table"));
    const ariaTables = Array.from(document.querySelectorAll("[role='table'], [role='grid']"))
      .filter(t => !t.closest("table")
        && !t.parentElement?.closest("[role='table'], [role='grid']"));
    const containers = [...tables, ...ariaTables].slice(0, MAX_TABLES);

    let dataRows = 0;
    containers.forEach((table, index) => {
      if (dataRows >= MAX_TABLE_DATA_ROWS) return;
      const isHidden = isHiddenForExtraction(table);
      if (isHidden) return;

      // Direct rows only: `closest` keeps a nested table's rows out of its
      // parent's data while preserving each table's own boundary marker.
      const rows = Array.from(table.querySelectorAll("tr, [role='row']")).filter(r => {
        const owner = r.closest("table, [role='table'], [role='grid']");
        return owner === table;
      });
      if (!rows.length) return;

      const cellsOf = (row: Element): string[] =>
        Array.from(row.children)
          .filter(c => c.matches("th, td, [role='cell'], [role='columnheader'], [role='rowheader'], [role='gridcell']"))
          .map(cellText)
          .filter(Boolean)
          .slice(0, MAX_TABLE_COLUMNS);

      const headerRow = rows.find(r =>
        Array.from(r.children).some(c => c.matches("th, [role='columnheader']"))) ?? null;
      const headerCells = headerRow ? cellsOf(headerRow) : [];
      out.push(`[Table ${index + 1}]${headerCells.length ? " " + headerCells.join(" | ") : ""}`);

      for (const row of rows) {
        if (dataRows >= MAX_TABLE_DATA_ROWS) break;
        if (row === headerRow) continue;
        const cells = cellsOf(row);
        if (cells.length >= 2) {
          out.push(`| ${cells.join(" | ")} |`);
          dataRows++;
        }
      }
    });
    return out;
  } catch {
    return [];
  }
}

const MONTH_NAMES = [
  "january", "february", "march", "april", "may", "june",
  "july", "august", "september", "october", "november", "december",
];

interface DateParts {
  iso: string;
  year: number;
  month: number;
  day: number;
}

function parseISODate(raw: string | undefined): DateParts | null {
  const m = (raw ?? "").trim().match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  const probe = new Date(Date.UTC(year, month - 1, day));
  if (probe.getUTCFullYear() !== year || probe.getUTCMonth() !== month - 1 || probe.getUTCDate() !== day) {
    return null;
  }
  const iso = `${m[1]}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  return { iso, year, month, day };
}

/** Recognisable renderings of one date, used only for full-date matching. */
function dateAliases(p: DateParts): string[] {
  const month = MONTH_NAMES[p.month - 1];
  const short = month.slice(0, 3);
  const d = String(p.day);
  const mm = String(p.month).padStart(2, "0");
  const dd = d.padStart(2, "0");
  return [
    p.iso,
    `${p.day}/${p.month}/${p.year}`,
    `${dd}/${mm}/${p.year}`,
    `${p.day}-${p.month}-${p.year}`,
    `${dd}-${mm}-${p.year}`,
    `${month} ${p.day}, ${p.year}`,
    `${month} ${p.day} ${p.year}`,
    `${p.day} ${month} ${p.year}`,
    `${short} ${p.day}, ${p.year}`,
    `${p.day} ${short} ${p.year}`,
  ].map(s => s.toLowerCase());
}

function containsAny(text: string, needles: string[]): boolean {
  return needles.some(n => text.includes(n));
}

/**
 * A picker click is only successful when the resulting value can be observed.
 * Calendar widgets differ widely, so this accepts an input value or an
 * explicit selected/current marker and otherwise reports failure rather than
 * guessing.
 */
async function verifyDateSelection(p: DateParts, cell: HTMLElement): Promise<boolean> {
  await sleep(150);
  const aliases = dateAliases(p);
  const inputs = Array.from(document.querySelectorAll("input")) as HTMLInputElement[];
  if (inputs.some(i => i.value === p.iso || aliases.includes(i.value.trim().toLowerCase()))) {
    return true;
  }
  if (cell.isConnected) {
    const cls = typeof cell.className === "string" ? cell.className.toLowerCase() : "";
    if (
      cell.getAttribute("aria-selected") === "true" ||
      cell.getAttribute("aria-current") === "date" ||
      cell.getAttribute("data-selected") === "true" ||
      /\b(selected|active|current)\b/.test(cls)
    ) {
      return true;
    }
  }
  return false;
}

// Overlays a hover can reveal. Kept in sync with the executor's fingerprint;
// text is bounded because every observation is provider-bound after redaction.
const OVERLAY_SELECTOR = [
  "[role='tooltip']", "[role='menu']", "[role='dialog']", "[role='listbox']",
  "[role='tree']", "[aria-expanded='true']", "details[open]", "dialog[open]",
  "[data-state='open']", "[data-popper-placement]",
  ".tooltip", ".dropdown-menu", ".submenu", ".popover", ".menu",
].join(",");

function extractOverlayText(): string {
  try {
    const seen: Element[] = [];
    const chunks: string[] = [];
    let total = 0;
    document.querySelectorAll(OVERLAY_SELECTOR).forEach(el => {
      if (!(el instanceof HTMLElement)) return;
      if (isHiddenForExtraction(el)) return;
      // A nested overlay adds no text an ancestor has not already provided.
      if (seen.some(parent => parent === el || parent.contains(el))) return;
      const text = visibleText(el);
      if (!text) return;
      seen.push(el);
      chunks.push(text);
      total += text.length;
    });
    return chunks.join("\n").slice(0, 1200);
  } catch {
    return "";
  }
}

window.__navigatorAgent = {
  list: () => {
    registry.clear();
    return candidates();
  },
  getArticleContent: () => extractArticleContent(),
  getTableRows: () => extractTableRows(),
  getOverlayText: () => extractOverlayText(),
  getCoords: (targetId, name) => {
    const el = elementFor(targetId, name);
    if (!el) return null;
    try {
      el.scrollIntoView({ block: "center", behavior: "instant" });
    } catch {}
    const r = el.getBoundingClientRect();
    return {
      x: Math.round(r.left + Math.max(1, r.width / 2)),
      y: Math.round(r.top + Math.max(1, r.height / 2)),
      width: Math.round(r.width),
      height: Math.round(r.height),
    };
  },
  /**
   * Activation point for a click: the resolved element, promoted to its chat
   * row when the planner matched an inner label. Always the true centre.
   */
  getClickPoint: (targetId, name) => {
    const el = elementFor(targetId, name);
    if (!el) return null;
    const target = rowFor(el) || el;
    try {
      target.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    } catch {}
    const r = target.getBoundingClientRect();
    return {
      x: Math.round(r.left + Math.max(1, r.width / 2)),
      y: Math.round(r.top + Math.max(1, r.height / 2)),
      width: Math.round(r.width),
      height: Math.round(r.height),
    };
  },
  /**
   * Confirm the activation point still belongs to the resolved target.
   * Called between CDP mouseMoved and mousePressed: a `mouseover` handler can
   * remove the control, and the press must not land on whatever replaces it.
   */
  hitTest: (targetId, name, x, y) => {
    const el = elementFor(targetId, name);
    if (!el || !el.isConnected) return { ok: false, error: "STALE_TARGET" };
    if (typeof x !== "number" || typeof y !== "number") return { ok: true };
    const target = rowFor(el) || el;
    const hit = document.elementFromPoint(x, y) as HTMLElement | null;
    if (!hit) return { ok: false, error: "STALE_TARGET" };
    const same = target === hit || target.contains(hit) || hit.contains(target);
    return same ? { ok: true } : { ok: false, error: "STALE_TARGET" };
  },
  click: (targetId, name) => {
    // Specialized WhatsApp handling: clear a stale search filter before
    // clicking Groups or a chat row.
    if (window.location.hostname.includes("whatsapp.com")) {
      const nm = (name || "").toLowerCase();
      if (nm === "groups" || nm === "group" || nm.includes("group") || nm.includes("chat")) {
        const cancelSearch = document.querySelector('[data-testid="chat-list-search-esc-button"], [data-icon="x-alt"], button[aria-label*="cancel search" i]') as HTMLElement | null;
        if (cancelSearch && !nm.includes("cancel")) {
          try { (cancelSearch.closest("button") || cancelSearch).click(); } catch {}
        }
      }
    }
    const el = elementFor(targetId, name);
    if (!el) return { ok: false, error: "STALE_TARGET" };
    try {
      el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    } catch {}

    // Activate exactly ONE element: the control itself, or its nearest
    // interactive ancestor. Dispatching on the leaf, target, and container
    // (plus anchor click) fired the same action several times.
    const clickable = (
      el.closest('button, [role="button"], a[href], [role="row"], [role="listitem"], [role="gridcell"], [data-testid*="cell-frame"], [data-testid*="list-item"]') as HTMLElement | null
    ) || el;
    try { clickable.focus?.(); } catch {}

    const r = clickable.getBoundingClientRect();
    const cx = Math.round(r.left + Math.max(1, r.width / 2));
    const cy = Math.round(r.top + Math.max(1, r.height / 2));
    const base: PointerEventInit = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX: cx,
      clientY: cy,
      screenX: cx,
      screenY: cy + 100,
      button: 0,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    };

    clickable.dispatchEvent(new PointerEvent("pointerover", base));
    clickable.dispatchEvent(new PointerEvent("pointerenter", base));
    clickable.dispatchEvent(new PointerEvent("pointerdown", { ...base, buttons: 1 }));
    clickable.dispatchEvent(new MouseEvent("mousedown", { ...base, buttons: 1 }));
    clickable.dispatchEvent(new PointerEvent("pointerup", { ...base, buttons: 0 }));
    clickable.dispatchEvent(new MouseEvent("mouseup", { ...base, buttons: 0 }));

    // One activation click. Native .click() dispatches exactly one click and
    // performs default actions (link navigation, form controls).
    try {
      clickable.click();
    } catch {
      clickable.dispatchEvent(new MouseEvent("click", { ...base, buttons: 0, detail: 1 }));
    }
    return { ok: true };
  },
  clickCoords: (x, y) => {
    const el = document.elementFromPoint(x, y) as HTMLElement | null;
    if (!el) return { ok: false, error: "STALE_TARGET" };
    try {
      el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    } catch {}

    const cx = Math.round(x);
    const cy = Math.round(y);

    const base: PointerEventInit = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX: cx,
      clientY: cy,
      screenX: cx,
      screenY: cy + 100,
      button: 0,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    };

    el.dispatchEvent(new PointerEvent("pointerdown", { ...base, buttons: 1 }));
    el.dispatchEvent(new MouseEvent("mousedown", { ...base, buttons: 1 }));
    el.dispatchEvent(new PointerEvent("pointerup", { ...base, buttons: 0 }));
    el.dispatchEvent(new MouseEvent("mouseup", { ...base, buttons: 0 }));
    try {
      el.click();
    } catch {
      el.dispatchEvent(new MouseEvent("click", { ...base, buttons: 0, detail: 1 }));
    }
    return { ok: true, targetId: el.id || undefined };
  },
  type: (targetId, text, clear, name) => {
    // Framework-owned nodes (React containers, Lexical editors) can throw from
    // inside property setters and selection APIs. Never let that escape as an
    // unhandled exception: report it so the executor can surface STALE_TARGET.
    try {
      let el = elementFor(targetId, name);
      const nm = (name || "").toLowerCase();

      // Specialized WhatsApp Web element resolution:
      if (window.location.hostname.includes("whatsapp.com")) {
        if (nm.includes("type") || nm.includes("message") || nm.includes("composer") || !el) {
          const waComposer = document.querySelector('footer [contenteditable="true"], [data-tab="10"], [data-testid="conversation-compose-box-input"]') as HTMLElement | null;
          if (waComposer) el = waComposer;
        } else if (nm.includes("search") || nm.includes("find")) {
          const waSearch = document.querySelector('div[data-tab="3"], [data-testid="chat-list-search"], [aria-label="Search input textbox"]') as HTMLElement | null;
          if (waSearch) el = waSearch;
          if (clear || !text) {
            const cancelSearch = document.querySelector('[data-testid="chat-list-search-esc-button"], [data-icon="x-alt"], [data-icon="back"], button[aria-label*="cancel search" i]') as HTMLElement | null;
            if (cancelSearch) {
              try { (cancelSearch.closest("button") || cancelSearch).click(); } catch {}
            }
          }
        }
      }

      if (el && !(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el.isContentEditable)) {
        const inner = el.querySelector("input, textarea, [contenteditable='true'], [contenteditable]") as HTMLElement | null;
        if (inner) el = inner;
      }
      if (!el) {
        const active = document.activeElement as HTMLElement | null;
        if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement || active?.isContentEditable) {
          el = active;
        }
      }
      // A rich-text editor often keeps the caret in a child node while the
      // planner targeted the wrapper: trust the focused editable.
      if (!el) return { ok: false, error: "STALE_TARGET" };
      const active = document.activeElement as HTMLElement | null;
      const editable =
        el.isContentEditable
        || el.getAttribute("contenteditable") === "true"
        || active?.isContentEditable === true;
      if (
        editable
        && !(el instanceof HTMLInputElement)
        && !(el instanceof HTMLTextAreaElement)
        && active?.isContentEditable
        && !el.isContentEditable
      ) {
        el = active;
      }

      if (el.isContentEditable || el.getAttribute("contenteditable") === "true") {
        el.focus();
        try {
          const sel = window.getSelection();
          const range = document.createRange();
          range.selectNodeContents(el);
          if (clear) {
            sel?.removeAllRanges();
            sel?.addRange(range);
            document.execCommand("delete", false);
          } else {
            // Caret at the end so existing text is preserved.
            range.selectNodeContents(el);
            range.collapse(false);
            sel?.removeAllRanges();
            sel?.addRange(range);
          }
        } catch {}

        let inserted = false;
        try {
          inserted = document.execCommand("insertText", false, text);
        } catch {}
        if (!inserted || !el.textContent?.includes(text)) {
          // Last resort: write the text and emit the same InputEvent real
          // typing produces, so framework listeners still update state.
          el.textContent = (clear ? "" : el.textContent || "") + text;
          try {
            el.dispatchEvent(new InputEvent("beforeinput", {
              bubbles: true, cancelable: true, inputType: "insertText", data: text,
            }));
          } catch {}
          el.dispatchEvent(new InputEvent("input", {
            bubbles: true, cancelable: true, inputType: "insertText", data: text,
          }));
        }
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        const input = el as HTMLInputElement | HTMLTextAreaElement;
        input.focus();
        if (clear) input.value = "";
        const val = (clear ? "" : input.value) + text;
        const proto = input instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
        try {
          if (setter) setter.call(input, val);
          else input.value = val;
        } catch {
          // Some input types reject direct assignment; the native setter is
          // the last attempt before reporting failure.
          input.value = val;
        }
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }

      // Submission is intentionally NOT done here. The executor performs one
      // and only one send/submit trigger, avoiding double-sent messages.
      return { ok: true };
    } catch (e) {
      // Bounded: a framework error can echo the value it rejected, and this
      // string travels back to the backend.
      const detail = String((e as Error)?.message ?? e).slice(0, 120);
      return { ok: false, error: `type failed: ${detail}` };
    }
  },
  /**
   * Local read-back for type verification. The value is returned to the
   * executor for comparison only and is never placed in an observation.
   */
  readValue: (targetId, name) => {
    const el = elementFor(targetId, name);
    if (!el || !el.isConnected) return { ok: false, error: "STALE_TARGET" };
    const isEditable = (node: HTMLElement | null): boolean =>
      !!node && (
        node instanceof HTMLInputElement
        || node instanceof HTMLTextAreaElement
        || node.isContentEditable
        || node.getAttribute("contenteditable") === "true"
      );
    const read = (node: HTMLElement): string => {
      if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) return node.value;
      if (node.isContentEditable || node.getAttribute("contenteditable") === "true") {
        return String(node.innerText ?? node.textContent ?? "");
      }
      const inner = (node as HTMLElement).querySelector(
        "input, textarea, [contenteditable='true'], [contenteditable]",
      ) as HTMLElement | null;
      return inner ? read(inner) : "";
    };
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    let node: HTMLElement = el;
    if (!isEditable(el)) {
      const inner = el.querySelector(
        "input, textarea, [contenteditable='true'], [contenteditable]",
      ) as HTMLElement | null;
      node = inner && isEditable(inner) ? inner : (active && isEditable(active) ? active : el);
    } else if (el.isContentEditable && active && isEditable(active)
      && (active === el || el.contains(active) || active.contains(el))) {
      node = active;
    }
    return { ok: true, value: read(node).slice(0, 10000) };
  },
  submit: (targetId, name) => {
    const el = elementFor(targetId, name) ||
      (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    if (!el) return { ok: false, error: "STALE_TARGET" };

    // 1. A real form is the safest single-submit path.
    const input = el as HTMLInputElement;
    if (input.form) {
      try {
        if (typeof input.form.requestSubmit === "function") {
          input.form.requestSubmit();
        } else {
          input.form.submit();
        }
        return { ok: true };
      } catch {
        // fall through to a single Enter dispatch
      }
    }

    // 2. Otherwise dispatch exactly one Enter sequence.
    try { el.focus?.(); } catch {}
    const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keypress", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
    return { ok: true };
  },
  scroll: (direction, containerId) => {
    const container = containerId ? elementFor(containerId) : null;
    const px = Math.round((container ? container.clientHeight : innerHeight) * 0.8);
    if (direction === "top") {
      if (container) container.scrollTop = 0;
      else scrollTo({ top: 0 });
    } else if (direction === "bottom") {
      if (container) container.scrollTop = container.scrollHeight;
      else scrollTo({ top: document.body.scrollHeight });
    } else if (container) {
      container.scrollTop += direction === "down" ? px : -px;
    } else {
      scrollBy({ top: direction === "down" ? px : -px });
    }
    return {
      ok: true,
      scrollY: Math.round(container ? container.scrollTop : (window.scrollY || document.scrollingElement?.scrollTop || 0)),
    };
  },
  hover: (targetId, name) => {
    const el = elementFor(targetId, name);
    if (!el) return { ok: false, error: "STALE_TARGET" };
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
    el.dispatchEvent(new MouseEvent("mouseenter", opts));
    el.dispatchEvent(new MouseEvent("mouseover", opts));
    el.dispatchEvent(new MouseEvent("mousemove", opts));
    return { ok: true };
  },
  selectDate: async (dateStr, targetId, name) => {
    const parts = parseISODate(dateStr);
    if (!parts) return { ok: false, error: "date must be provided as YYYY-MM-DD" };

    // 1. Native date inputs are the only fully reliable case: set the value
    //    through the native setter so framework change handlers fire.
    const explicit = targetId || name ? elementFor(targetId || "", name) : null;
    const nativeInput = explicit instanceof HTMLInputElement && explicit.type === "date"
      ? explicit
      : explicit
        ? (explicit.querySelector("input[type='date']") as HTMLInputElement | null)
        : (document.querySelector("input[type='date']") as HTMLInputElement | null);
    if (nativeInput) {
      if (nativeInput.disabled || nativeInput.readOnly) {
        return { ok: false, error: "date input is not editable" };
      }
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      try {
        if (setter) setter.call(nativeInput, parts.iso);
        else nativeInput.value = parts.iso;
      } catch {
        return { ok: false, error: "could not write the date input value" };
      }
      nativeInput.dispatchEvent(new Event("input", { bubbles: true }));
      nativeInput.dispatchEvent(new Event("change", { bubbles: true }));
      await sleep(120);
      if (nativeInput.value !== parts.iso) {
        return { ok: false, error: `date input kept "${nativeInput.value}" instead of "${parts.iso}"` };
      }
      return { ok: true, value: parts.iso };
    }

    // 2. Calendar pickers. Match full-date labels/attributes first; only fall
    //    back to a bare day number when exactly one visible calendar shows the
    //    requested month, so a shared "15" across two months cannot win.
    const aliases = dateAliases(parts);
    const collectCalendarRoots = (): HTMLElement[] => {
      const all = (Array.from(document.querySelectorAll(
        "[role='dialog'], [role='grid'], [class*='calendar' i], [class*='datepicker' i], [class*='date-picker' i], [data-testid*='calendar' i], [data-testid*='datepicker' i]",
      )) as HTMLElement[]).filter(r => !isHiddenForExtraction(r));
      return all.filter(r => !all.some(other => other !== r && other.contains(r)));
    };
    let calendarRoots = collectCalendarRoots();
    if (!calendarRoots.length && explicit) {
      // The target may be the button that opens the picker. One click to open
      // is the only side effect; the date itself is still verified below.
      try { explicit.click(); } catch {}
      await sleep(150);
      calendarRoots = collectCalendarRoots();
    }
    const roots = calendarRoots.length
      ? calendarRoots
      : (document.body ? [document.body] : []);

    const fullDateMatches = new Set<HTMLElement>();
    const dayOnlyMatches = new Set<HTMLElement>();
    const dayLabel = String(parts.day);

    for (const root of roots) {
      const nodes = Array.from(root.querySelectorAll(
        "[role='gridcell'], [role='option'], [data-day], [data-date], td, button, [aria-label]",
      ));
      // A bare day number is only meaningful inside a calendar whose visible
      // text also names the requested month and year.
      const rootText = (root.textContent || "").toLowerCase();
      const showsMonth = rootText.includes(MONTH_NAMES[parts.month - 1]) &&
        rootText.includes(String(parts.year));

      for (const node of nodes) {
        if (!(node instanceof HTMLElement) || isHiddenForExtraction(node)) continue;
        const dataDate = (node.getAttribute("data-date") || "").trim().toLowerCase();
        const aria = (node.getAttribute("aria-label") || "").trim().toLowerCase();
        const text = (node.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
        const dataDay = (node.getAttribute("data-day") || "").trim().toLowerCase();

        if (dataDate === parts.iso || containsAny(aria, aliases) || containsAny(text, aliases)) {
          fullDateMatches.add(node);
        } else if (showsMonth && (dataDay === dayLabel || text === dayLabel)) {
          dayOnlyMatches.add(node);
        }
      }
    }

    // A parent cell and its inner button often both carry the date label.
    // Keep only the innermost match so nesting does not look like ambiguity.
    const innermost = (matches: Set<HTMLElement>): HTMLElement[] => {
      const all = Array.from(matches);
      return all.filter(el => !all.some(other => other !== el && el.contains(other)));
    };
    const fullMatches = innermost(fullDateMatches);
    const dayMatches = innermost(dayOnlyMatches);
    const candidates = fullMatches.length ? fullMatches : dayMatches;
    if (candidates.length > 1) {
      return { ok: false, error: `date ${parts.iso} is ambiguous (${candidates.length} matching day cells)` };
    }
    const cell = candidates[0];
    if (!cell) {
      return {
        ok: false,
        error: `could not find ${parts.iso} in an open date picker (month navigation is not automated)`,
      };
    }

    try { cell.scrollIntoView({ block: "center", behavior: "instant" }); } catch {}
    cell.click();
    if (!(await verifyDateSelection(parts, cell))) {
      return { ok: false, error: `clicked ${parts.iso} but the selection could not be verified` };
    }
    return { ok: true, value: parts.iso };
  },
  sliderAdjust: async (targetId, name, percent, direction) => {
    const el = elementFor(targetId, name);
    if (!el) return { ok: false, code: "STALE_TARGET", error: "STALE_TARGET" };
    const hasPercent = typeof percent === "number" && Number.isFinite(percent);
    const requestPercent = hasPercent ? Math.max(0, Math.min(100, percent as number)) : null;

    if (el instanceof HTMLInputElement && el.type === "range") {
      const min = parseFloat(el.min || "0");
      const max = parseFloat(el.max || "100");
      const step = parseFloat(el.step || "1");
      if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
        return { ok: false, code: "NO_EFFECT", error: "range input has no usable min/max" };
      }

      let newVal: number;
      if (requestPercent !== null) {
        newVal = min + (max - min) * (requestPercent / 100);
      } else if (direction) {
        const current = parseFloat(el.value);
        if (!Number.isFinite(current)) {
          return { ok: false, code: "NO_EFFECT", error: "range input has no numeric value" };
        }
        newVal = current + (direction === "left" ? -1 : 1) * (Number.isFinite(step) && step > 0 ? step * 5 : 1);
      } else {
        return { ok: false, error: "slider requires params.percent or params.direction" };
      }
      if (Number.isFinite(step) && step > 0) {
        newVal = min + Math.round((newVal - min) / step) * step;
      }
      newVal = Number(Math.max(min, Math.min(max, newVal)).toFixed(6));

      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      if (setter) setter.call(el, String(newVal));
      else el.value = String(newVal);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      await sleep(120);

      const finalVal = parseFloat(el.value);
      if (!Number.isFinite(finalVal)) {
        return { ok: false, code: "NO_EFFECT", error: "slider has no numeric value after adjustment" };
      }
      const tolerance = Number.isFinite(step) && step > 0 ? step / 2 + 1e-6 : 1e-6;
      if (Math.abs(finalVal - newVal) > tolerance) {
        return {
          ok: false,
          code: "NO_EFFECT",
          error: `slider settled on ${finalVal} instead of requested ${newVal}`,
        };
      }
      return { ok: true, value: finalVal };
    }

    // 3. ARIA sliders: request an absolute percentage by pointing at the
    //    matching x coordinate, then read aria-valuenow back.
    const isAriaSlider = el.getAttribute("role") === "slider" || el.hasAttribute("aria-valuenow");
    if (!isAriaSlider) {
      return { ok: false, error: "target is not a range or ARIA slider" };
    }
    const min = parseFloat(el.getAttribute("aria-valuemin") ?? "0");
    const max = parseFloat(el.getAttribute("aria-valuemax") ?? "100");
    const current = parseFloat(el.getAttribute("aria-valuenow") ?? "");
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
      return { ok: false, code: "NO_EFFECT", error: "ARIA slider has no usable aria-valuemin/aria-valuemax" };
    }

    let target: number;
    if (requestPercent !== null) {
      target = min + (max - min) * (requestPercent / 100);
    } else if (direction) {
      if (!Number.isFinite(current)) {
        return { ok: false, code: "NO_EFFECT", error: "ARIA slider has no numeric aria-valuenow" };
      }
      target = current + (direction === "left" ? -1 : 1) * ((max - min) * 0.05);
    } else {
      return { ok: false, error: "slider requires params.percent or params.direction" };
    }
    target = Math.max(min, Math.min(max, target));

    const rect = el.getBoundingClientRect();
    if (!(rect.width > 0)) {
      return { ok: false, code: "NO_EFFECT", error: "ARIA slider has no measurable width" };
    }
    const ratio = (target - min) / (max - min);
    const x = rect.left + rect.width * ratio;
    const y = rect.top + rect.height / 2;
    const PointerCtor: typeof MouseEvent = typeof PointerEvent !== "undefined"
      ? (PointerEvent as unknown as typeof MouseEvent)
      : MouseEvent;
    const init = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
    const pointerInit = { ...init, pointerId: 1, isPrimary: true, pointerType: "mouse", buttons: 1 } as PointerEventInit;
    el.dispatchEvent(new PointerCtor("pointerdown", pointerInit));
    el.dispatchEvent(new MouseEvent("mousedown", { ...init, buttons: 1 }));
    el.dispatchEvent(new PointerCtor("pointermove", pointerInit));
    el.dispatchEvent(new MouseEvent("mousemove", { ...init, buttons: 1 }));
    el.dispatchEvent(new PointerCtor("pointerup", { ...pointerInit, buttons: 0 }));
    el.dispatchEvent(new MouseEvent("mouseup", { ...init, buttons: 0 }));
    await sleep(150);

    const finalNow = parseFloat(el.getAttribute("aria-valuenow") ?? "");
    if (!Number.isFinite(finalNow)) {
      return { ok: false, code: "NO_EFFECT", error: "ARIA slider did not expose a numeric aria-valuenow" };
    }
    if (requestPercent !== null) {
      const achieved = ((finalNow - min) / (max - min)) * 100;
      if (Math.abs(achieved - requestPercent) > 2) {
        return {
          ok: false,
          code: "NO_EFFECT",
          error: `ARIA slider settled at ${achieved.toFixed(1)}% instead of requested ${requestPercent}%`,
        };
      }
    } else if (direction && Math.sign(finalNow - current) !== (direction === "left" ? -1 : 1)) {
      return {
        ok: false,
        code: "NO_EFFECT",
        error: `ARIA slider did not move ${direction} from ${current}`,
      };
    }
    return { ok: true, value: finalNow };
  },
};
