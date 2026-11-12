import { semanticElement, checkTargetFreshness, queryAllWithShadow as semanticQuery, accessibleName as fullAccessibleName, viewportBounds } from "../src/semantic";
import type { SemanticElement } from "../src/contracts";
/**
 * Content script injected programmatically by the service worker.
 * Performs in-page interaction and returns serializable results.
 * Exposes window.__navigatorAgent for executeScript probes.
 */
export {}

interface AgentElement {
  targetId: string;
  semantic?: SemanticElement;
  role: string;
  name: string;
  tag: string;
  inputType?: string;
  /** Explicit semantic classification; never used to overwrite DOM attributes. */
  search?: boolean;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds: { x: number; y: number; width: number; height: number };
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

interface NavigatorAgent {
  list: () => AgentElement[];
  click: (targetId: string, name?: string) => { ok: boolean; error?: string };
  clickCoords: (x: number, y: number) => { ok: boolean; targetId?: string; error?: string };
  getCoords: (targetId: string, name?: string) => { x: number; y: number; width: number; height: number } | null;
  getClickPoint: (targetId: string, name?: string) => { x: number; y: number; width: number; height: number } | null;
  type: (targetId: string, text: string, clear: boolean, name?: string) => { ok: boolean; error?: string; value?: string };
  submit: (targetId: string, name?: string) => { ok: boolean; error?: string };
  readValue: (targetId: string, name?: string) => ValueProbe;
  pageState: () => PageStateProbe;
  scroll: (direction: "up" | "down" | "top" | "bottom", containerId?: string) => { ok: boolean; scrollY: number };
  extractText: (maxLen?: number) => string;
  selectOption: (targetId: string, option: string, name?: string) => Promise<{ ok: boolean; error?: string; value?: string }>;
  setDate: (targetId: string, date: string, name?: string) => Promise<{ ok: boolean; error?: string; value?: string }>;
  uploadFile: (targetId: string, fileName: string, base64: string, name?: string) => { ok: boolean; error?: string; value?: string };
}

declare global {
  interface Window { __navigatorAgent?: NavigatorAgent }
}

const registry = new Map<string, HTMLElement>();

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

function normText(value: string | null | undefined): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

function escapeSelector(value: string): string {
  try {
    return CSS.escape(value);
  } catch {
    return value.replace(/([^\w-])/g, "\\$1");
  }
}

function rootById(el: Element, id: string): Element | null {
  try {
    const root = el.getRootNode() as Document | ShadowRoot;
    if (root && "getElementById" in root) {
      const found = (root as Document).getElementById(id);
      if (found) return found;
    }
    if (root && "querySelector" in root) {
      const found = (root as ShadowRoot).querySelector(`#${escapeSelector(id)}`);
      if (found) return found;
    }
  } catch {}
  try {
    return document.getElementById(id);
  } catch {
    return null;
  }
}

/** aria-labelledby ids can point into the same shadow root as the control. */
function labelledByText(el: Element): string {
  const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
  if (ids.length === 0) return "";
  return normText(ids.map(id => rootById(el, id)?.textContent || "").join(" "));
}

/** Native <label for> / wrapping label, including shadow-DOM labels. */
function associatedLabelText(el: HTMLElement): string {
  const input = el as HTMLInputElement;
  try {
    if (input.labels && input.labels.length > 0) {
      return normText(Array.from(input.labels).map(l => l.textContent || "").join(" "));
    }
  } catch {}
  const id = (el.id || "").trim();
  if (id) {
    try {
      const root = el.getRootNode() as Document | ShadowRoot;
      const label = root && "querySelector" in root
        ? (root as ParentNode).querySelector(`label[for="${escapeSelector(id)}"]`)
        : document.querySelector(`label[for="${escapeSelector(id)}"]`);
      if (label) return normText(label.textContent);
    } catch {}
  }
  return normText(el.closest("label")?.textContent);
}

/** Human-readable accessible name, preserving the page's own wording. */
function accessibleName(el: HTMLElement): string {
  const computed = fullAccessibleName(el);
  if (computed) return computed;
  const labelled = labelledByText(el);
  if (labelled) return labelled;
  const ariaLabel = normText(el.getAttribute("aria-label"));
  if (ariaLabel) return ariaLabel;
  const label = associatedLabelText(el);
  if (label) return label;
  const title = normText(el.getAttribute("title"));
  if (title) return title;
  const input = el as HTMLInputElement;
  return normText(input.placeholder);
}

function isTextEntry(el: HTMLElement): boolean {
  const tag = el.tagName.toLowerCase();
  if (tag === "textarea" || el.isContentEditable || el.getAttribute("contenteditable") === "true") return true;
  if (tag !== "input") return false;
  const type = (el as HTMLInputElement).type.toLowerCase();
  return !["button", "submit", "reset", "image", "file", "checkbox", "radio", "range", "color", "hidden", "password"].includes(type);
}

/**
 * Stable attribute evidence for a control (id, name, type, wrapping search form).
 * This is evidence, not a replacement for the accessible name.
 */
function attributeEvidence(el: HTMLElement): string {
  const input = el as HTMLInputElement;
  const bits: string[] = [
    el.id || "",
    input.name || "",
    input.type || "",
    el.getAttribute("role") || "",
    el.getAttribute("autocomplete") || "",
    el.getAttribute("class") || "",
  ];
  const form = el.closest("form");
  if (form) {
    bits.push(
      form.getAttribute("role") || "",
      form.getAttribute("action") || "",
      form.id || "",
      form.getAttribute("name") || "",
      form.getAttribute("class") || "",
    );
  }
  return bits.filter(Boolean).join(" ");
}

function insideSearchForm(el: HTMLElement): boolean {
  const form = el.closest('form[role="search"], form[action*="search" i], form[id*="search" i], form[name*="search" i], [role="search"]');
  return Boolean(form);
}

/**
 * Explicit semantic classification for search fields/buttons.
 * The original name and DOM attributes are preserved; this only adds a flag
 * (and, for text fields, the standard searchbox role in the observation).
 */
function isSearchControl(el: HTMLElement, name: string): boolean {
  const tag = el.tagName.toLowerCase();
  const role = (el.getAttribute("role") || "").toLowerCase();
  const input = el as HTMLInputElement;
  const type = tag === "input" ? (input.type || "text").toLowerCase() : "";
  const textEntry = isTextEntry(el);
  const buttonLike = tag === "button" || role === "button" || type === "submit";

  if (type === "search" || role === "searchbox") return true;
  if (!textEntry && !buttonLike) return false;

  const evidence = `${name} ${attributeEvidence(el)}`.toLowerCase();
  const signal = /(?:^|[^a-z])(search|query|keyword|keywords|find)(?:[^a-z]|$)/i.test(evidence)
    || /(?:^|\s)q(?:\s|$)/i.test(attributeEvidence(el))
    || insideSearchForm(el);
  return signal;
}

function derivedNameHint(el: HTMLElement): string {
  const input = el as HTMLInputElement;
  return normText([input.name, el.id, input.type].filter(Boolean).join(" "));
}

/**
 * Distinguishing hint for a search field the page never named.
 *
 * Only the form field name is used: ids like "plain-search" add noise without
 * disambiguating, while name="field-keywords" is the page's own stable handle.
 */
function searchFallbackHint(el: HTMLElement): string {
  return normText((el as HTMLInputElement).name || "");
}

/** Every stable identity string the page exposes for an element. */
function identityText(el: HTMLElement): string {
  const input = el as HTMLInputElement;
  return normText([
    accessibleName(el),
    el.getAttribute("data-navigator-id"),
    input.name,
    el.id,
    input.type,
    el.textContent,
  ].filter(Boolean).join(" ")).toLowerCase();
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
    'button._2doB4z',
    'button._2KpZ6l._2doB4z',
    '[data-testid*="close"]',
    '[aria-label="Close" i]',
    '[aria-label*="dismiss" i]',
    '#onetrust-accept-btn-handler',
    'button[id*="cookie"]',
    'button[id*="accept"]',
  ].join(', ');

  const nodes = semanticQuery(selectors);
  const list: AgentElement[] = [];

  nodes.forEach((n, i) => {
    const el = n as HTMLElement;
    const r = el.getBoundingClientRect();
    const mapped = viewportBounds(el);
    if (!mapped) return;
    const style = window.getComputedStyle(el);
    let ancestor: Element | null = el;
    let isHidden = false;
    while (ancestor) {
      const computed = getComputedStyle(ancestor);
      if (computed.display === "none" || computed.visibility === "hidden" || Number(computed.opacity) === 0 || computed.fontSize === "0px" || Number.parseInt(computed.zIndex, 10) <= -999 || ancestor.getAttribute("aria-hidden") === "true") { isHidden = true; break; }
      ancestor = ancestor.parentElement || ((ancestor.getRootNode() as ShadowRoot).host ?? null);
    }
    const hasSize = r.width > 0 && r.height > 0;
    if (isHidden || !hasSize) return;

    const input = el as HTMLInputElement;
    const rawId = el.id ? el.id.trim() : "";
    const targetId = rawId || `nav-el-${i}`;

    const textTrimmed = (el.textContent || "").trim();
    const isCloseGlyph = textTrimmed === "✕" || textTrimmed === "×" || textTrimmed === "X" || textTrimmed.toLowerCase() === "close";
    const isInModalOrDialog = Boolean(el.closest('[role="dialog"], [aria-modal="true"], .modal, ._2KpZ6l, [class*="dialog" i], [class*="modal" i], [class*="overlay" i], [class*="popup" i]'));
    const isExplicitClose = el.matches('button._2doB4z, button._2KpZ6l._2doB4z, button.close, [data-testid*="close"], [aria-label*="close" i], [aria-label*="dismiss" i]');
    const isOverlayClose = (isCloseGlyph && isInModalOrDialog) || isExplicitClose || el.matches('.ytp-ad-overlay-close-button');

    const isAdSkip = el.matches('.ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button, [id^="skip-button"]');
    const isVoiceBtn = el.matches('[data-icon="ptt"], [aria-label*="voice" i], [aria-label*="audio" i], [aria-label*="record" i]');
    const isSendBtn = !isVoiceBtn && el.matches('[data-testid="compose-btn-send"], [data-icon="send"], span[data-icon="send"], button[aria-label="Send" i], button[aria-label*="Send message" i]');
    const isComposer = el.matches('footer [contenteditable="true"], [data-tab="10"], [data-testid="conversation-compose-box-input"], footer [role="textbox"]');
    const isCancelVoice = el.matches('[data-icon="cancel"], [data-testid="btn-cancel"], [data-icon="delete-clip"], button[aria-label*="cancel" i]');

    // Exclude repetitive header/footer navigation category clutter unless it's a search input, submit or modal close
    const isNavOrFooter = Boolean(el.closest('footer, #navFooter, #nav-subnav, #nav-belt, .s-navigation-left-panel, [role="navigation"]'));
    const isSpecialControl = isAdSkip || isOverlayClose || el.tagName.toLowerCase() === "input" || el.tagName.toLowerCase() === "textarea" || (el.getAttribute("role") === "searchbox");
    if (isNavOrFooter && !isSpecialControl) {
      return;
    }

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

    const anchor = (el.tagName.toLowerCase() === "a" ? el : el.closest("a[href]")) as HTMLAnchorElement | null;
    const href = anchor?.href || undefined;

    const accessible = accessibleName(el);
    const semanticFallback = (
      (el.getAttribute("data-icon") ? `icon ${el.getAttribute("data-icon")}` : "") ||
      (el.getAttribute("data-testid") && !el.textContent?.trim() ? el.getAttribute("data-testid") : "") ||
      normText(el.textContent) ||
      normText(input.value) ||
      derivedNameHint(el)
    );
    let name = (
      (isAdSkip ? "Skip Ad " : "") +
      (isOverlayClose && !el.textContent?.trim() ? "Close " : "") +
      (accessible || semanticFallback)
    ).replace(/\s+/g, " ").trim().slice(0, 140);

    if (isAdSkip && !name) name = "Skip Ad";
    if (isOverlayClose && (!name || isCloseGlyph)) name = "Close";
    if (isVoiceBtn) name = "Voice message";
    if (isCancelVoice) name = "Cancel Recording";
    if (isSendBtn) name = "Send";
    if (isComposer && (!name || name.length > 50)) name = "Type a message";
    if (isWaGroupFilter) name = "Groups";
    if (isWaCancelSearch) name = "Cancel search";
    if (isWaSearch && !name) name = "Search or start new chat";
    if (isWaChatRow && waChatTitle) name = waChatTitle;

    // Semantic classification, not a blind rename. A name the page actually
    // exposed (label, aria-label, title, placeholder) is kept verbatim. Only an
    // unnamed search field gets a "Search" fallback plus its form-field name,
    // so Amazon's #twotabsearchtextbox is discoverable without altering what
    // Flipkart or a labelled search input already says.
    const searchControl = isSearchControl(el, name);
    if (searchControl && !accessible) {
      const hint = searchFallbackHint(el);
      name = `Search${hint ? ` (${hint})` : ""}`;
    }
    name = name.slice(0, 140);

    // Discard generic ad badges and empty link elements
    const nameLower = name.toLowerCase();
    if (nameLower === "sponsored" || nameLower === "ad" || nameLower === "feedback" || nameLower === "conditions of use") {
      return;
    }
    if (el.tagName.toLowerCase() === "a" && !name && !el.querySelector('img')) {
      return;
    }

    // Group product card title and price together for e-commerce
    if (href && (href.includes("/dp/") || href.includes("/gp/product/") || href.includes("/p/"))) {
      const card = el.closest('[data-component-type="s-search-result"], .s-result-item, [data-id], ._1AtVbE, ._75nlfW, [data-asin]');
      if (card) {
        const priceEl = card.querySelector('.a-price .a-offscreen, ._30jeq3, .Nx9bqj, .priceToPay, [data-feature-name="corePrice"]');
        const priceText = priceEl?.textContent?.trim() || "";
        if (priceText && !name.includes(priceText)) {
          name = `${name} - ${priceText}`;
        }
      }
    }

    el.setAttribute("data-navigator-id", targetId);
    registry.set(targetId, el);
    if (rawId) {
      registry.set(`nav-el-${i}`, el);
    }

    const role = isAdSkip ? "ad-skip" :
                 isOverlayClose ? "close-button" :
                 isComposer ? "textbox" :
                 isWaSearch ? "textbox" :
                 (searchControl && isTextEntry(el)) ? "searchbox" :
                 (isSendBtn || isVoiceBtn || isCancelVoice || isWaGroupFilter || isWaCancelSearch) ? "button" :
                 (isWaChatRow ? "listitem" : (el.getAttribute("role") || el.tagName.toLowerCase()));
    const cssSelector = rawId ? `#${CSS.escape(rawId)}` : `[data-navigator-id="${targetId}"]`;

    list.push({
      targetId,
      role,
      name,
      tag: el.tagName.toLowerCase(),
      inputType: el.tagName.toLowerCase() === "input" ? input.type : undefined,
      search: searchControl || undefined,
      cssSelector,
      enabled: !(input as HTMLButtonElement).disabled && !el.hasAttribute("aria-disabled"),
      visible: true,
      href,
      bounds: mapped,
      semantic: semanticElement(el, targetId) || undefined,
    });
  });

  return list;
}

function isEditableElement(el: HTMLElement): boolean {
  return el instanceof HTMLInputElement
    || el instanceof HTMLTextAreaElement
    || el instanceof HTMLSelectElement
    || el.isContentEditable
    || el.getAttribute("contenteditable") === "true";
}

function elementFor(targetId: string, name?: string, requireEditable = false): HTMLElement | null {
  const cid = cleanId(targetId);
  const pick = (node: HTMLElement | null | undefined): HTMLElement | null =>
    node && node.isConnected && (!requireEditable || isEditableElement(node)) &&
    (!name || fullAccessibleName(node).toLowerCase() === name.toLowerCase() ||
      (node.getAttribute("data-navigator-id") === cid && !fullAccessibleName(node))) ? node : null;

  const registered = pick(registry.get(cid)) ?? pick(registry.get(targetId));
  if (registered) return registered;

  // 1. Direct query by data-navigator-id
  let el = cid ? (document.querySelector(`[data-navigator-id="${CSS.escape(cid)}"]`) as HTMLElement | null) : null;
  if (pick(el)) return el;

  // 2. Direct query by ID or name attribute
  if (cid) {
    el = document.getElementById(cid);
    if (pick(el)) return el;
    el = document.querySelector(`[name="${CSS.escape(cid)}"]`) as HTMLElement | null;
    if (pick(el)) return el;
  }

  // 3. Search within open Shadow Roots
  if (cid) {
    const shadowNodes = queryAllWithShadow(document, `[data-navigator-id="${CSS.escape(cid)}"], #${CSS.escape(cid)}`);
    if (shadowNodes.length > 0 && pick(shadowNodes[0] as HTMLElement)) {
      return shadowNodes[0] as HTMLElement;
    }
  }

  // 4. Refresh candidates if registry was empty
  candidates();
  const refreshed = pick(registry.get(cid));
  if (refreshed) return refreshed;

  // 5. Re-resolve React/Vue replacements using semantic identity. Ambiguous
  // names fail closed rather than clicking an arbitrary replacement.
  const recovered = checkTargetFreshness(cid, undefined, name || undefined);
  if (pick(recovered as HTMLElement | null)) return recovered as HTMLElement;

  // 6. Fallback search by accessible name / textContent
  if (name && name.trim()) {
    const wanted = name.trim().toLowerCase();
    const all = queryAllWithShadow(document, 'button, input, textarea, select, a[href], [role="button"], [role="link"], [role="textbox"], [role="searchbox"], [role="row"], [role="listitem"], [role="gridcell"], [tabindex], [data-testid], span[title], div[title], ytd-thumbnail, #video-title') as HTMLElement[];
    // Typing/reading a value may never bind to a same-named button or link.
    const pool = requireEditable ? all.filter(isEditableElement) : all;

    // Specialized WhatsApp resolution:
    if (window.location.hostname.includes("whatsapp.com")) {
      if (wanted === "groups" || wanted === "group" || wanted.includes("group filter")) {
        const groupsBtn = pool.find(e => {
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

    // Exact or substring match on the full accessible identity (aria-labelledby,
    // aria-label, associated label, title, placeholder, name/id/type, text).
    const matches = pool.filter(e => {
      const txt = identityText(e);
      return txt === wanted || txt.includes(wanted) || (txt.length >= 3 && wanted.includes(txt));
    });
    if (matches.length === 1) return matches[0];
    if (matches.length > 1) return null;

    // Word token match
    const tokens = wanted.split(/\s+/).filter(t => t.length > 2);
    if (tokens.length > 0) {
      const tokenMatches = pool.filter(e => {
        const txt = identityText(e);
        return tokens.every(tok => txt.includes(tok));
      });
      if (tokenMatches.length === 1) return tokenMatches[0];
    }
  }

  return null;
}

function editableValue(el: HTMLElement): string {
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) {
    return el.value;
  }
  if (el.isContentEditable || el.getAttribute("contenteditable") === "true") {
    return el.textContent || "";
  }
  const nested = el.querySelector("input, textarea, select, [contenteditable='true'], [contenteditable]") as HTMLElement | null;
  return nested ? editableValue(nested) : "";
}

function computePageState(): PageStateProbe {
  const main = document.querySelector('main, [role="main"], #content, #centerCol, #dp, body') as HTMLElement | null;
  const text = normText(main?.innerText || document.body?.innerText || "");
  let hash = 0;
  const limit = Math.min(text.length, 4000);
  for (let i = 0; i < limit; i++) {
    hash = ((hash * 31) + text.charCodeAt(i)) | 0;
  }
  const resultSelectors = [
    '[data-component-type="s-search-result"]',
    '.s-result-item',
    '[data-testid*="result" i]',
    '[class*="result" i]',
    'article',
    '[role="listitem"]',
  ].join(", ");
  return {
    url: window.location.href,
    title: document.title,
    readyState: document.readyState,
    textLength: text.length,
    textHash: hash,
    interactiveCount: document.querySelectorAll('button, input, textarea, a[href], [role="button"]').length,
    resultCount: document.querySelectorAll(resultSelectors).length,
  };
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

window.__navigatorAgent = {
  list: () => {
    registry.clear();
    return candidates();
  },
  getCoords: (targetId, name) => {
    const el = elementFor(targetId, name);
    if (!el) return null;
    try {
      el.scrollIntoView({ block: "center", behavior: "instant" });
    } catch {}
    const r = viewportBounds(el);
    if (!r) return null;
    return {
      x: Math.round(r.x + Math.max(1, r.width / 2)),
      y: Math.round(r.y + Math.max(1, r.height / 2)),
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
    const r = viewportBounds(target);
    if (!r) return null;
    return {
      x: Math.round(r.x + Math.max(1, r.width / 2)),
      y: Math.round(r.y + Math.max(1, r.height / 2)),
      width: Math.round(r.width),
      height: Math.round(r.height),
    };
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
      let el = elementFor(targetId, name, true);
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

      if (el && !(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement || el.isContentEditable)) {
        const inner = el.querySelector("input, textarea, select, [contenteditable='true'], [contenteditable]") as HTMLElement | null;
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
      } else if (el instanceof HTMLSelectElement) {
        // Native <select> filters: typing into a select does nothing — the
        // operation that matters is selecting the matching option so the
        // site's change handler fires. Match by value or visible label.
        const wanted = text.trim().toLowerCase();
        let matched = Array.from(el.options).find(o => o.value.trim().toLowerCase() === wanted)
          ?? Array.from(el.options).find(o => (o.textContent || "").trim().toLowerCase() === wanted)
          ?? Array.from(el.options).find(o => (o.textContent || "").trim().toLowerCase().includes(wanted) && wanted.length > 2);
        if (!matched) {
          return { ok: false, error: `no option matching "${text.slice(0, 60)}" in select` };
        }
        el.focus();
        const proto = HTMLSelectElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
        try {
          if (setter) setter.call(el, matched.value);
          else el.value = matched.value;
        } catch {
          el.value = matched.value;
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
      return { ok: true, value: editableValue(el) };
    } catch (e) {
      // Bounded: a framework error can echo the value it rejected, and this
      // string travels back to the backend.
      const detail = String((e as Error)?.message ?? e).slice(0, 120);
      return { ok: false, error: `type failed: ${detail}` };
    }
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
  readValue: (targetId, name) => {
    const el = elementFor(targetId, name, true) ||
      (!targetId && !name && document.activeElement instanceof HTMLElement && isEditableElement(document.activeElement)
        ? document.activeElement : null);
    if (!el) return { ok: false, error: "STALE_TARGET" };
    return {
      ok: true,
      value: editableValue(el),
      tag: el.tagName.toLowerCase(),
      inputType: el.tagName.toLowerCase() === "input" ? (el as HTMLInputElement).type : undefined,
      editable: el.isContentEditable || el.getAttribute("contenteditable") === "true"
        || el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement,
    };
  },
  pageState: () => computePageState(),
  selectOption: async (targetId, option, name) => {
    const control = elementFor(targetId, name);
    if (!control) return { ok: false, error: "STALE_TARGET" };
    if (control instanceof HTMLSelectElement) {
      return window.__navigatorAgent!.type(targetId, option, true, name);
    }
    control.click();
    const wanted = normText(option).toLowerCase();
    // Portals are often siblings of the app root, outside the control.
    const selector = '[role="option"], [role="menuitem"], [data-value], li[tabindex], [role="listbox"] li';
    let matches: HTMLElement[] = [];
    for (let i = 0; i < 20; i++) {
      matches = queryAllWithShadow(document, selector).filter((el): el is HTMLElement =>
        el instanceof HTMLElement && el.getBoundingClientRect().width > 0 &&
        (normText(el.getAttribute("aria-label") || el.textContent).toLowerCase() === wanted ||
          normText(el.getAttribute("data-value")).toLowerCase() === wanted));
      if (matches.length) break;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    if (matches.length !== 1) return { ok: false, error: matches.length ? "ambiguous option" : "option not found" };
    matches[0].click();
    await new Promise(resolve => setTimeout(resolve, 40));
    const display = normText(control.textContent || control.getAttribute("aria-label") || "").toLowerCase();
    const value = control instanceof HTMLInputElement ? control.value : control.getAttribute("data-value") || display;
    const selected = control.getAttribute("aria-activedescendant") === matches[0].id ||
      control.getAttribute("aria-valuetext")?.toLowerCase() === wanted ||
      normText(value).toLowerCase().includes(wanted);
    return selected ? { ok: true, value } : { ok: false, error: "selection not reflected in control" };
  },
  setDate: async (targetId, date, name) => {
    const control = elementFor(targetId, name);
    if (!control) return { ok: false, error: "STALE_TARGET" };
    if (control instanceof HTMLInputElement && control.type === "date") {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(control, date);
      control.dispatchEvent(new Event("input", { bubbles: true }));
      control.dispatchEvent(new Event("change", { bubbles: true }));
      return control.value === date ? { ok: true, value: date } : { ok: false, error: "date value did not persist" };
    }
    // Text calendars: obey the input's advertised format. A custom grid is
    // handled only when a unique date-labelled cell is exposed by the site.
    if (control instanceof HTMLInputElement && control.type === "text") {
      const format = control.placeholder.toLowerCase();
      const [year, month, day] = date.split("-");
      const formatted = format.includes("dd/mm") ? `${day}/${month}/${year}`
        : format.includes("mm/dd") ? `${month}/${day}/${year}` : date;
      const result = window.__navigatorAgent!.type(targetId, formatted, true, name);
      return result.ok && result.value === formatted ? { ok: true, value: formatted } : { ok: false, error: "date value did not persist" };
    }
    control.click();
    const day = String(Number(date.slice(-2)));
    const [year, month] = date.split("-");
    const dateLabels = [date, `${month}/${day}/${year}`, `${day}/${month}/${year}`];
    for (let move = 0; move < 24; move++) {
      const candidates = queryAllWithShadow(document, '[data-date], [aria-label], [role="gridcell"]')
        .filter((el): el is HTMLElement => el instanceof HTMLElement && el.getBoundingClientRect().width > 0 &&
          (dateLabels.includes(el.getAttribute("data-date") || "") ||
            dateLabels.some(label => (el.getAttribute("aria-label") || "").toLowerCase().includes(label.toLowerCase()))));
      if (candidates.length === 1) {
        candidates[0].click();
        const value = control instanceof HTMLInputElement ? control.value : normText(control.textContent);
        return value ? { ok: true, value } : { ok: false, error: "date selection not reflected" };
      }
      if (candidates.length > 1) return { ok: false, error: "ambiguous calendar date" };
      const title = document.querySelector('[aria-live="polite"], [class*="month"], [data-calendar-month]')?.textContent || "";
      const shown = Date.parse(`1 ${title}`);
      if (Number.isNaN(shown)) break;
      const desired = Number(year) * 12 + Number(month) - 1;
      const current = new Date(shown).getFullYear() * 12 + new Date(shown).getMonth();
      if (current === desired) break;
      const direction = desired > current ? "next" : "previous";
      const buttons = queryAllWithShadow(document, 'button[aria-label], [role="button"][aria-label]')
        .filter((el): el is HTMLElement => el instanceof HTMLElement && new RegExp(direction, "i").test(el.getAttribute("aria-label") || ""));
      if (buttons.length !== 1) break;
      buttons[0].click();
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return { ok: false, error: "calendar date not found or not verifiable" };
  },
  uploadFile: (targetId, fileName, base64, name) => {
    const control = elementFor(targetId, name, true);
    if (!(control instanceof HTMLInputElement) || control.type !== "file") return { ok: false, error: "file input not found" };
    try {
      const raw = atob(base64);
      if (raw.length > 5 * 1024 * 1024) return { ok: false, error: "file exceeds 5 MiB" };
      const bytes = Uint8Array.from(raw, ch => ch.charCodeAt(0));
      const file = new File([bytes], fileName);
      const transfer = new DataTransfer();
      transfer.items.add(file);
      control.files = transfer.files;
      control.dispatchEvent(new Event("input", { bubbles: true }));
      control.dispatchEvent(new Event("change", { bubbles: true }));
      return control.files?.length === 1 && control.files[0].name === fileName && control.files[0].size === bytes.length
        ? { ok: true, value: fileName } : { ok: false, error: "file selection not reflected" };
    } catch { return { ok: false, error: "file selection failed" }; }
  },
  scroll: (direction, containerId) => {
    let container = containerId ? elementFor(containerId) : null;
    if (containerId && !container) return { ok: false, scrollY: 0 };
    if (!container) {
      const active = document.activeElement as HTMLElement | null;
      const hovered = document.querySelector(":hover:last-of-type") as HTMLElement | null;
      for (let node = active || hovered; node && node !== document.body; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 2) {
          container = node;
          break;
        }
      }
      if (!container) {
        const regions = queryAllWithShadow(document, '[role="dialog"], [role="listbox"], aside, [aria-modal="true"]');
        container = regions.find((node): node is HTMLElement => node instanceof HTMLElement &&
          /(auto|scroll)/.test(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight + 2) || null;
      }
    }
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
  extractText: (maxLen = 4000) => {
    return extractReadableText(maxLen);
  },
};

function extractReadableText(maxLen = 4000): string {
  try {
    const root =
      document.querySelector(
        'main, [role="main"], article, #content, #mw-content-text, #centerCol, #dp, body'
      ) || document.body;
    if (!root) return "";

    const ignoreTags = new Set(["SCRIPT", "STYLE", "NAV", "FOOTER", "HEADER", "SVG", "NOSCRIPT", "IFRAME", "OBJECT"]);
    const lines: string[] = [];
    let currentLength = 0;

    const elements = root.querySelectorAll(
      'h1, h2, h3, h4, p, table, [data-feature-name], .a-price, .a-price-whole, #corePriceDisplay_desktop_feature_div, #corePrice_desktop, #apex_desktop, .priceToPay, [data-feature-name="corePrice"], [data-feature-name="title"], #productTitle, #twister, #productDescription, #feature-bullets, #techSpecs-table, #productDetails_techSpec_section_1, #productDetails_db_sections, ' +
      '._30jeq3, .Nx9bqj, ._16Jk6d, ._21A06W, ._1mX5Pr, ._3k-BhJ, ._2160x1, ._35KyD6, ._4rR01T, .KzDlHZ, .B_NuCI, ' +
      '[role="listitem"], .pIav2d, .JVOxCe, .yR1fYc, .U3gHgb, [jsname="I3kE2b"], .YMlIz, [data-hotel-id], .D90Uje, .b0G24d, .qA0Zcf, .W8L26d, ' +
      '[class*="text-label-"], [class*="text-sd-"], [class*="ranking"], [class*="rating"], [data-difficulty], [data-cypress="UserAvatar"], [class*="badge"], [class*="stat"]'
    );

    if (elements.length === 0) {
      const text = (root as HTMLElement).innerText || "";
      return text.slice(0, maxLen).trim();
    }

    const seenText = new Set<string>();

    for (const el of Array.from(elements)) {
      if (currentLength >= maxLen) break;
      if (ignoreTags.has(el.tagName)) continue;

      const htmlEl = el as HTMLElement;
      if (htmlEl.offsetParent === null && htmlEl.tagName !== "BODY") {
        continue;
      }

      if (el.closest('nav, footer, header, [role="navigation"], #cookie-banner, .cookie-notice, #nav-main')) {
        continue;
      }

      let blockText = "";
      if (el.tagName === "TABLE") {
        const rows = Array.from(el.querySelectorAll("tr"))
          .map(r =>
            Array.from(r.querySelectorAll("th, td"))
              .map(c => (c.textContent || "").trim())
              .filter(Boolean)
              .join(" | ")
          )
          .filter(Boolean);
        blockText = rows.join("\n");
      } else {
        blockText = (htmlEl.innerText || htmlEl.textContent || "").trim();
      }

      blockText = blockText.replace(/[ \t]+/g, " ").trim();
      if (!blockText || blockText.length < 5) continue;
      if (seenText.has(blockText)) continue;
      seenText.add(blockText);

      if (/^H[1-4]$/.test(el.tagName)) {
        blockText = `### ${blockText}`;
      }

      lines.push(blockText);
      currentLength += blockText.length + 1;
    }

    return lines.join("\n\n").slice(0, maxLen).trim();
  } catch {
    return "";
  }
}
