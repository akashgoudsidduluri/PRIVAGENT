import { defineConfig } from "vite";
import { copyManifest, iifeWrapper } from "./vite.shared";

// Content-script entry — one of the three single-input configs (see
// vite.config.ts). The IIFE wrapper makes the bundle a classic
// script, as MV3 content scripts must be; with a single input there
// is no shared chunk for the wrapper to choke on. emptyOutDir is
// false: the background config (run first) already emptied dist/.
export default defineConfig({
  plugins: [iifeWrapper(), copyManifest()],
  build: {
    outDir: "dist",
    emptyOutDir: false,
    rollupOptions: {
      input: { content: "lib/content.ts" },
      output: { entryFileNames: "[name].js", format: "es" },
    },
  },
});
