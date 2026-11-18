/** Small DOM-free helpers for rendering untrusted page/model text.
 *
 * Everything that leaves the backend is untrusted: page titles, evidence
 * strings and final answers all echo remote content. These helpers escape
 * first and only turn validated http(s) URLs into anchors.
 */

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, ch => HTML_ESCAPES[ch] ?? ch);
}

/** Returns a normalized http(s) URL, or null for javascript:/data:/inline
 * credentials and malformed strings. */
export function safeHttpUrl(raw: string): string | null {
  try {
    const u = new URL(raw);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (!u.hostname) return null;
    if (u.username || u.password) return null;
    return u.href;
  } catch {
    return null;
  }
}

/** Only raster data URLs or bare base64 are accepted for the inspector. */
export function safeImageSrc(screenshot: string | null | undefined): string | null {
  if (!screenshot) return null;
  const s = screenshot.trim();
  if (/^data:image\/(?:jpeg|jpg|png|webp|gif);base64,[A-Za-z0-9+/=\s]+$/i.test(s)) return s;
  if (/^[A-Za-z0-9+/=\s]+$/.test(s)) return `data:image/jpeg;base64,${s.replace(/\s+/g, "")}`;
  return null;
}

export function linkLabel(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "") || "Open page";
  } catch {
    return "Open page";
  }
}

/** Emphasis for already-escaped plain text. Must only ever run on
 * escaped text with no markup in it: the regexes match `_`/`*` pairs,
 * and a built `<a href="...my_file_name.pdf" target="_blank">` tag
 * contains exactly those characters — running emphasis on built HTML
 * rewrote the href into `my<em>file</em>name.pdf` (a 404) and mangled
 * every bare `https://…/a_b_c` link. URLs are therefore never passed
 * through here; linkify applies this to the text gaps only. */
function emphasizeHtml(escaped: string): string {
  return escaped
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
}

/** Escape the whole string, then wrap markdown/bare http(s) links.
 *
 * With `emphasize`, bold/italic is rendered in the text gaps and link
 * labels (already-escaped plain text) — never in the URLs or the
 * anchors built from them. Direct callers that render step/console
 * text keep the default (no emphasis), exactly as before. */
export function linkify(text: string, emphasize = false): string {
  const re = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<"')]+)/g;
  const parts: string[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  const esc = (s: string): string => emphasize ? emphasizeHtml(escapeHtml(s)) : escapeHtml(s);
  while ((m = re.exec(text)) !== null) {
    parts.push(esc(text.slice(last, m.index)));
    const label = m[1] ?? m[3];
    const href = safeHttpUrl(m[2] ?? m[3]);
    if (href) {
      // A markdown link's label is prose and may carry emphasis; a
      // bare URL's label IS the URL and must never be emphasised.
      const labelHtml = m[1] !== undefined ? esc(label) : escapeHtml(label);
      parts.push(
        `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${labelHtml} ↗</a>`
      );
    } else {
      parts.push(escapeHtml(m[0]));
    }
    last = re.lastIndex;
  }
  parts.push(esc(text.slice(last)));
  return parts.join("");
}

/** Escape a table row and split it into trimmed cells. */
function splitTableRow(line: string): string[] {
  return line.replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim());
}

/** Render Markdown images ![alt](url) as <img>, everything else via linkify.
 *
 * An image URL is only accepted when `safeHttpUrl` passes it
 * (credential-free http(s)) — javascript:/data: URLs render as inert
 * text, and the alt text is escaped like every other attribute value.
 * Loading the image fetches it from the store's image CDN; the backend
 * only ever supplies an og:image from a product page the agent
 * actually visited. Images are consumed here, before `linkify`, so
 * the `[alt](url)` inside `![alt](url)` is never also turned into a
 * link (with a stray `!` left in front of it).
 */
function renderInlineImages(part: string): string {
  const re = /!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g;
  const out: string[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(part)) !== null) {
    out.push(linkify(part.slice(last, m.index), true));
    const src = safeHttpUrl(m[2]);
    if (src) {
      out.push(`<img src="${escapeHtml(src)}" alt="${escapeHtml(m[1])}" loading="lazy" class="md-product-img">`);
    } else {
      out.push(escapeHtml(m[0]));
    }
    last = re.lastIndex;
  }
  out.push(linkify(part.slice(last), true));
  return out.join("");
}

/** Inline Markdown that cannot emit arbitrary HTML.
 *
 * Emphasis is rendered by `linkify(..., true)` on the escaped text
 * gaps only — never on built anchors or images (see emphasizeHtml).
 */
function renderInline(raw: string): string {
  return raw.split(/(`[^`]*`)/g).map(part => {
    if (part.length >= 2 && part.startsWith("`") && part.endsWith("`")) {
      return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
    }
    return renderInlineImages(part);
  }).join("");
}

/** Render model Markdown without trusting it.
 *
 * Supports headings, paragraphs, bold/italic, inline and fenced code, lists,
 * blockquotes, tables, links and images. Raw HTML is always escaped, so a model answer
 * cannot inject markup. Unknown syntax is shown as plain text.
 */
export function renderMarkdown(text: string): string {
  const lines = (text || "").replace(/\r\n?/g, "\n").split("\n");
  const out: string[] = [];
  let paragraph: string[] = [];
  let listType: "ul" | "ol" | null = null;
  let inCode = false;
  let codeLines: string[] = [];

  const flushParagraph = (): void => {
    if (paragraph.length > 0) {
      out.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
      paragraph = [];
    }
  };
  const closeList = (): void => {
    if (listType) {
      out.push(`</${listType}>`);
      listType = null;
    }
  };
  const flushCode = (): void => {
    if (inCode) {
      out.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      codeLines = [];
      inCode = false;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    if (/^```/.test(trimmed)) {
      flushParagraph();
      closeList();
      if (inCode) flushCode();
      else inCode = true;
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    if (!trimmed) {
      flushParagraph();
      closeList();
      continue;
    }

    // Pipe table: header row followed by a separator row.
    if (/^\|/.test(trimmed) && i + 1 < lines.length
        && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim())
        && lines[i + 1].includes("-")) {
      flushParagraph();
      closeList();
      const headers = splitTableRow(trimmed);
      const rows: string[][] = [];
      i += 1; // skip separator
      while (i + 1 < lines.length && /^\|/.test(lines[i + 1].trim())) {
        i += 1;
        rows.push(splitTableRow(lines[i].trim()));
      }
      out.push(
        `<table class="md-table"><thead><tr>${headers.map(h => `<th>${renderInline(h)}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows.map(row => `<tr>${headers.map((_, idx) => `<td>${renderInline(row[idx] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table>`
      );
      continue;
    }

    if (/^(?:---+|\*\*\*+|___+)$/.test(trimmed)) {
      flushParagraph();
      closeList();
      out.push("<hr>");
      continue;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.*)$/);
    if (unordered) {
      flushParagraph();
      if (listType !== "ul") {
        closeList();
        out.push("<ul>");
        listType = "ul";
      }
      out.push(`<li>${renderInline(unordered[1])}</li>`);
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ordered) {
      flushParagraph();
      if (listType !== "ol") {
        closeList();
        out.push("<ol>");
        listType = "ol";
      }
      out.push(`<li>${renderInline(ordered[1])}</li>`);
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      closeList();
      out.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
      continue;
    }

    closeList();
    paragraph.push(line);
  }

  flushParagraph();
  closeList();
  flushCode();
  return out.join("\n");
}
