import { defineConfig } from "vite";
import { copyManifest } from "./vite.shared";

// Background entry. Task 13: ONE entry per config file (content and
// companion have their own files: vite.content.config.ts,
// vite.companion.config.ts). With only one entry per build Rollup
// cannot create a shared chunk — every module an entry imports is
// inlined into that entry's own bundle, so a cross-entry import can
// never produce the top-level `import` the old multi-input config
// produced (the Moodle-fix landmine: a shared chunk import inside
// the IIFE wrapper around content.js broke the production build,
// twice). The proof is tests/viteSelfContainedEntries.test.ts, which
// runs all three builds and asserts each entry has no top-level
// import and dist/ holds exactly the three entry bundles.
//
// Ordering note: this config (run first by `npm run build`) is the
// only one with emptyOutDir — the configs build sequentially into
// the same outDir, so emptyOutDir on the later ones would delete the
// earlier entries. The manifest copy runs on every config
// (idempotent) so a single-entry build still produces a loadable
// extension.
export default defineConfig({
  plugins: [copyManifest()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: { background: "src/background.ts" },
      output: { entryFileNames: "[name].js", format: "es" },
    },
  },
});
