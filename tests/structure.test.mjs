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

t("headline present", html.includes("A browser agent that never leaks your screen."));
t("all 8 anchor targets exist", ["problem","solution","workflow","features","use-cases","tech","team","cta"].every(id => html.includes(`id="${id}"`)));
t("hero redaction visual present", html.includes("redact-flow") && html.includes("On-Device Redaction Layer") && html.includes("[CARD_REDACTED]"));
t("hero headline is the Editorial line", html.includes("A browser agent that never leaks your screen."));
t("fonts: Editorial stack (Source Serif 4 + Inter + JetBrains Mono), no Chakra Petch / IBM Plex Sans", css.includes("Source Serif 4") && css.includes("Inter") && css.includes("JetBrains Mono") && !css.includes("Chakra Petch") && !css.includes("IBM Plex Sans"));
t("reduced-motion block present", css.includes("prefers-reduced-motion"));
t("focus-visible styled", css.includes(":focus-visible"));
t("skip link present", html.includes("skip-link"));
t("mobile nav has aria-expanded", html.includes('aria-expanded="false"') && html.includes("nav__toggle"));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
