import { accessibleName, queryAllWithShadow, perceptible, semanticElement, viewportBounds } from "./semantic";
import type { BrowserObservation, InteractiveElement } from "./contracts";

const SENSITIVE_INPUT_TYPES = new Set(["password", "hidden"]);
const INTERACTIVE_SELECTORS =
  'button, input, textarea, select, a[href], [role="button"], [role="link"], [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"], [role="switch"]';
const MAX_TEXT_LENGTH = 120;

/** DOM walk usable both in-page (content script) and under jsdom in tests. */
export function collectInteractiveElements(
  viewportW: number,
  viewportH: number
): InteractiveElement[] {
  const nodes = queryAllWithShadow(INTERACTIVE_SELECTORS);
  const out: InteractiveElement[] = [];
  for (const node of nodes) {
    const el = node as HTMLElement;
    assertDomNode(el);
    const rect = el.getBoundingClientRect();
    const mapped = viewportBounds(el);
    if (!mapped) continue;
    if (rect.width === 0 || rect.height === 0) continue;
    if (rect.bottom < 0 || rect.right < 0 || rect.top > viewportH || rect.left > viewportW) continue;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || !perceptible(el)) continue;
    const input = el as HTMLInputElement;
    const inputType = el.tagName === "INPUT" ? input.type : undefined;
    const sensitive = inputType !== undefined && SENSITIVE_INPUT_TYPES.has(inputType);
    out.push({
      targetId: el.id || "",
      semantic: semanticElement(el, el.id || "") || undefined,
      role: explicitRole(el) || implicitRole(el),
      name: accessibleName(el),
      tag: el.tagName.toLowerCase(),
      inputType,
      value: sensitive ? undefined : trim(input?.value),
      visible: true,
      enabled: !(el as HTMLButtonElement).disabled,
      bounds: mapped,
    });
  }
  return out;
}

function assertDomNode(el: unknown): asserts el is HTMLElement {
  if (typeof Element === "undefined" || !(el instanceof Element)) {
    throw new Error("observer used outside a DOM document");
  }
}

function explicitRole(el: Element): string {
  const r = el.getAttribute("role");
  return r && r.length > 0 ? r : "";
}

function implicitRole(el: Element): string {
  switch (el.tagName.toLowerCase()) {
    case "button": return "button";
    case "a": return "link";
    case "input": {
      const t = (el as HTMLInputElement).type;
      if (t === "text" || t === "search" || t === "email" || t === "url" || t === "password") return "textbox";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      return "textbox";
    }
    case "textarea": return "textbox";
    case "select": return "combobox";
    default: return "generic";
  }
}

function trim(s: string | null | undefined): string | undefined {
  if (s === null || s === undefined) return undefined;
  const t = s.replace(/\s+/g, " ").trim();
  return t.length === 0 ? undefined : t.slice(0, MAX_TEXT_LENGTH);
}

/** Post-walk sanitizer. Fail-closed: re-strips every sensitive-typed value,
 * even if the walk somehow reported one. */
export function sanitizeObservation(obs: BrowserObservation): BrowserObservation {
  const interactive = obs.interactive.map(e =>
    e.inputType !== undefined && SENSITIVE_INPUT_TYPES.has(e.inputType)
      ? { ...e, value: undefined }
      : e
  );
  return { ...obs, interactive };
}
