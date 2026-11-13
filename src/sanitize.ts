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

/** Escape the whole string, then wrap markdown/bare http(s) links. */
export function linkify(text: string): string {
  const re = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<"')]+)/g;
  const parts: string[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    parts.push(escapeHtml(text.slice(last, m.index)));
    const label = m[1] ?? m[3];
    const href = safeHttpUrl(m[2] ?? m[3]);
    if (href) {
      parts.push(
        `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)} ↗</a>`
      );
    } else {
      parts.push(escapeHtml(m[0]));
    }
    last = re.lastIndex;
  }
  parts.push(escapeHtml(text.slice(last)));
  return parts.join("");
}

/** Escape a table row and split it into trimmed cells. */
function splitTableRow(line: string): string[] {
  return line.replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim());
}

/** Inline Markdown that cannot emit arbitrary HTML.
 *
 * `linkify` escapes the text before anchors are added, and every other
 * replacement below only inserts a fixed tag around already-escaped text.
 */
function renderInline(raw: string): string {
  return raw.split(/(`[^`]*`)/g).map(part => {
    if (part.length >= 2 && part.startsWith("`") && part.endsWith("`")) {
      return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
    }
    return linkify(part)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  }).join("");
}

/** Render model Markdown without trusting it.
 *
 * Supports headings, paragraphs, bold/italic, inline and fenced code, lists,
 * blockquotes, tables and links. Raw HTML is always escaped, so a model answer
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
