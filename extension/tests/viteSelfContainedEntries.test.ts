/** Task 13 — the vite config landmine, proven fixed.

The old multi-input vite config split any module imported by two
entries (e.g. ./semantic, imported by the content entry AND — via
targeting.ts — by the background entry) into a shared chunk that
content.js imported at the top level, and the IIFE wrapper around
content.js cannot contain a top-level import: the production build
broke (twice — the Moodle fix and its review). Each entry now has
its own single-input config (vite.config.ts,
vite.content.config.ts, vite.companion.config.ts), so Rollup cannot
create a shared chunk at all.

This test IS the proof: it runs the real `vite build` three times
(once per entry, as `npm run build` does) into a temp dir and
asserts (a) every build succeeds with the cross-entry import in
place, (b) the temp dist holds exactly the three entry bundles, and
(c) no entry contains a top-level `import` — so every bundle loads
in MV3 as a classic script / module service worker.
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const EXT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const VITE_BIN = join(EXT_ROOT, "node_modules", ".bin", "vite");
const TOP_LEVEL_IMPORT = /^import[\s({]/m;
const DYNAMIC_IMPORT = /\bimport\s*\(/;
// One build per entry, in `npm run build` order (background first —
// it is the only config with emptyOutDir).
const BUILDS: Array<{ name: string; config: string | null }> = [
  { name: "background", config: null },
  { name: "content", config: "vite.content.config.ts" },
  { name: "companion", config: "vite.companion.config.ts" },
];

describe("vite self-contained entries (Task 13)", () => {
  it("builds each entry alone and emits import-free bundles", () => {
    const outDir = mkdtempSync(join(tmpdir(), "privagent-vite-test-"));
    try {
      // The dedup under test: targeting.ts (background entry) imports
      // stripIconGlyphs from ./semantic (also used by the content
      // entry). If the config regressed to shared chunks, these builds
      // would emit a chunk import — or the IIFE wrapper would break.
      const targeting = readFileSync(join(EXT_ROOT, "src", "targeting.ts"), "utf-8");
      expect(targeting).toContain('from "./semantic"');
      // No local copy of the ranges/regex: the name survives only in
      // the comment explaining why the copy existed.
      expect(targeting).not.toContain("const TARGET_GLYPH_RANGES");
      expect(targeting).not.toContain("function targetGlyphRe");

      for (const { name, config } of BUILDS) {
        const args = ["build", "--outDir", outDir];
        if (config) args.splice(1, 0, "--config", config);
        const built = spawnSync(VITE_BIN, args, {
          cwd: EXT_ROOT, encoding: "utf-8", timeout: 180_000,
        });
        if (built.status !== 0) {
          throw new Error(`vite build (${name}) failed:\n${built.stdout}\n${built.stderr}`);
        }
      }

      const files = readdirSync(outDir).sort();
      expect(files).toEqual([
        "background.js",
        "companion.js",
        "content.js",
        "manifest.json",
      ]);
      for (const entry of ["background.js", "content.js", "companion.js"]) {
        const code = readFileSync(join(outDir, entry), "utf-8");
        expect(code).not.toMatch(TOP_LEVEL_IMPORT);
      }
      // The IIFE-wrapped entries are classic scripts: no dynamic
      // import either (there is nothing to import — no chunks exist).
      for (const entry of ["content.js", "companion.js"]) {
        const code = readFileSync(join(outDir, entry), "utf-8");
        expect(code).not.toMatch(DYNAMIC_IMPORT);
      }
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  }, 180_000);
});
