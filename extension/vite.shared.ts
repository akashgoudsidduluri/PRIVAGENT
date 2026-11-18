import { cpSync, mkdirSync } from "node:fs";
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
      const outDir = options.dir ?? "dist";
      cpSync("manifest.json", join(outDir, "manifest.json"));
      // The manifest's declarative_net_request.rule_resources points
      // at rules/trackers.json, so the rules file must ship in the
      // output too — without it Chrome refuses to load the unpacked
      // extension ("The provided path 'rules/trackers.json' is invalid").
      mkdirSync(join(outDir, "rules"), { recursive: true });
      cpSync("rules/trackers.json", join(outDir, "rules", "trackers.json"));
    },
  };
}
