import { cpSync } from "node:fs";
import { join } from "node:path";
import type { Plugin } from "vite";

// Task 13: shared build plugins for the three single-input entry
// configs (vite.config.ts, vite.content.config.ts,
// vite.companion.config.ts). See vite.config.ts for why each entry
// builds alone.
export function iifeWrapper(): Plugin {
  return {
    name: "iife-wrap-entry",
    apply: "build",
    renderChunk(code: string) {
      return `(() => {\n${code}\n})();`;
    },
  };
}

export function copyManifest(): Plugin {
  return {
    name: "copy-extension-static",
    apply: "build",
    // Task 13: writeBundle (not closeBundle) so the copy lands in the
    // ACTUAL output dir — a build with --outDir <tmp> (as the
    // self-contained-entries test does) used to copy the manifest
    // into dist/ instead, leaving the temp dist unloadable.
    writeBundle(options) {
      cpSync("manifest.json", join(options.dir ?? "dist", "manifest.json"));
    },
  };
}
