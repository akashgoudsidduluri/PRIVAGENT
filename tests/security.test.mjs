import test from "node:test";
import assert from "node:assert/strict";

import {
  escapeHtml,
  linkify,
  linkLabel,
  renderMarkdown,
  safeHttpUrl,
  safeImageSrc,
} from "../src/sanitize.ts";

test("escapeHtml neutralizes tags and attribute breakers", () => {
  const out = escapeHtml(`<img src=x onerror="alert(1)">&'`);
  assert.equal(out, "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;&#39;");
});

test("linkify renders malicious markdown titles as inert text", () => {
  const out = linkify("[<img src=x onerror=alert(1)>](https://example.com/a)");
  assert.ok(!out.includes("<img"));
  assert.ok(out.includes("&lt;img"));
  assert.ok(out.includes('href="https://example.com/a"'));
  assert.ok(out.includes('rel="noopener noreferrer"'));
});

test("linkify refuses javascript/data markdown links", () => {
  for (const raw of [
    "[click](javascript:alert(1))",
    "[click](data:text/html,<script>alert(1)</script>)",
  ]) {
    const out = linkify(raw);
    assert.ok(!out.includes("<a "), out);
  }
});

test("linkify escapes surrounding text and amp-encodes hrefs once", () => {
  const out = linkify(`look <b>here</b> https://example.com/?a=1&b=2`);
  assert.ok(out.includes("&lt;b&gt;here&lt;/b&gt;"));
  assert.ok(out.includes('href="https://example.com/?a=1&amp;b=2"'));
});

test("safeHttpUrl accepts only credential-free http(s)", () => {
  assert.equal(safeHttpUrl("https://example.com/x"), "https://example.com/x");
  assert.equal(safeHttpUrl("http://127.0.0.1:8000/"), "http://127.0.0.1:8000/");
  for (const bad of [
    "javascript:alert(1)",
    "data:text/html,x",
    "file:///etc/passwd",
    "https://user:pass@example.com/",
    "not a url",
  ]) {
    assert.equal(safeHttpUrl(bad), null, bad);
  }
});

test("safeImageSrc only allows raster data URLs or bare base64", () => {
  assert.equal(safeImageSrc("data:image/jpeg;base64,AAAA"), "data:image/jpeg;base64,AAAA");
  assert.equal(safeImageSrc("AAAA"), "data:image/jpeg;base64,AAAA");
  for (const bad of [
    "data:image/svg+xml;base64,PHN2Zz4=",
    "javascript:alert(1)",
    "https://evil.example/track.png",
    "data:text/html;base64,AAAA",
  ]) {
    assert.equal(safeImageSrc(bad), null, bad);
  }
});

test("linkLabel shows a host, not a claim about the retailer", () => {
  assert.equal(linkLabel("https://www.amazon.in/dp/x"), "amazon.in");
  assert.equal(linkLabel("https://example.com/"), "example.com");
  assert.equal(linkLabel("nonsense"), "Open page");
});

test("renderMarkdown never emits raw HTML from model text", () => {
  const out = renderMarkdown(
    "## <img src=x onerror=alert(1)>\n\nText <script>alert(2)</script> here."
  );
  assert.ok(!out.includes("<img"), out);
  assert.ok(!out.includes("<script"), out);
  assert.ok(out.includes("&lt;img"));
  assert.ok(out.includes("&lt;script&gt;"));
});

test("renderMarkdown renders headings, emphasis, lists and tables", () => {
  const out = renderMarkdown(
    "### Capabilities\n\n- **Bold** item\n- second\n\n| Store | Price |\n| --- | --- |\n| Amazon | 499 |"
  );
  assert.ok(out.includes("<h3>Capabilities</h3>"), out);
  assert.ok(out.includes("<ul>") && out.includes("<li><strong>Bold</strong> item</li>"), out);
  assert.ok(out.includes("<th>Store</th>") && out.includes("<td>Amazon</td>"), out);
});

test("renderMarkdown escapes fenced code and inline code", () => {
  const out = renderMarkdown("```\n<b>code</b>\n```\n\nUse `x < y` inline.");
  assert.ok(out.includes("<pre><code>&lt;b&gt;code&lt;/b&gt;</code></pre>"), out);
  assert.ok(out.includes("<code>x &lt; y</code>"), out);
});

test("renderMarkdown keeps markdown links inert when the scheme is unsafe", () => {
  const out = renderMarkdown("[click](javascript:alert(1))");
  assert.ok(!out.includes("href=\"javascript"), out);
  assert.ok(!out.includes("<a "), out);

  const safe = renderMarkdown("[docs](https://example.com/a)");
  assert.ok(safe.includes('href="https://example.com/a"'), safe);
});

test("renderMarkdown renders product images and refuses unsafe image URLs", () => {
  const out = renderMarkdown(
    "![Samsung Galaxy Fit3 — ₹3,899](https://m.media-amazon.com/images/I/fit3.jpg)"
  );
  assert.ok(out.includes('<img src="https://m.media-amazon.com/images/I/fit3.jpg"'), out);
  assert.ok(out.includes('alt="Samsung Galaxy Fit3 — ₹3,899"'), out);
  assert.ok(out.includes('class="md-product-img"'), out);

  // An image is not also turned into a link with a stray "!" in front.
  assert.ok(!out.includes("!"), out);

  // Unsafe image URLs render as inert text, never an <img>.
  for (const bad of [
    "![x](javascript:alert(1))",
    "![x](data:image/png;base64,AAAA)",
    "![x](https://user:pass@cdn.example.com/x.jpg)",
  ]) {
    assert.ok(!renderMarkdown(bad).includes("<img"), bad);
  }

  // Alt text is escaped like any other attribute value.
  const quoted = renderMarkdown('![a"b](https://cdn.example.com/x.jpg)');
  assert.ok(quoted.includes('alt="a&quot;b"'), quoted);
});

test("renderMarkdown keeps underscores in links intact (no emphasis inside hrefs)", () => {
  // Frontend flaw-hunt finding 1: the italic regex ran on the built
  // HTML, so the `_` in target="_blank" paired with the `_` in
  // my_file_name.pdf and rewrote the href to my<em>file</em>name.pdf.
  const out = renderMarkdown("[the doc](https://example.com/my_file_name.pdf)");
  assert.ok(out.includes('href="https://example.com/my_file_name.pdf"'), out);
  assert.ok(!out.includes("<em>"), out);
  const bare = renderMarkdown("see https://example.com/a_b_c for details");
  assert.ok(bare.includes('href="https://example.com/a_b_c"'), bare);
  assert.ok(!bare.includes("<em>"), bare);
});

test("renderMarkdown still renders emphasis in plain text", () => {
  const out = renderMarkdown("This is *very* important and **bold** too, plus _italic_ text.");
  assert.ok(out.includes("<em>very</em>"), out);
  assert.ok(out.includes("<strong>bold</strong>"), out);
  assert.ok(out.includes("<em>italic</em>"), out);
});
