import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (p) => readFileSync(join(root, p), "utf8");

let pass = 0, fail = 0;
const t = (name, cond) => {
  if (cond) { pass++; console.log(`  PASS ${name}`); }
  else { fail++; console.error(`  FAIL ${name}`); }
};

console.log("structure tests");

const html = read("index.html");
const css = read("src/styles/main.css");

t("headline present", html.includes("That") && html.includes("Respects Your Privacy"));
t("all 8 anchor targets exist", ["problem","solution","workflow","features","use-cases","tech","team","cta"].every(id => html.includes(`id="${id}"`)));
t("hero redaction visual present", html.includes("redact-flow") && html.includes("LOCAL REDACTION LAYER") && html.includes("[CARD_REDACTED]"));
t("hero image file exists", existsSync(join(root, "public/images/hero-space-02.png")));
t("all 3 images staged", ["hero-space-01","hero-space-02","hero-space-03"].every(n => existsSync(join(root, `public/images/${n}.png`))));
t("fonts: no Inter/Roboto", !/font-family[^;]*(Inter|Roboto|Arial)/.test(css));
t("reduced-motion block present", css.includes("prefers-reduced-motion"));
t("focus-visible styled", css.includes(":focus-visible"));
t("skip link present", html.includes("skip-link"));
t("mobile nav has aria-expanded", html.includes('aria-expanded="false"') && html.includes("nav__toggle"));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
