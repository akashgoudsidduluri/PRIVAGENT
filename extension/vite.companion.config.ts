import { defineConfig } from "vite";
import { copyManifest, iifeWrapper } from "./vite.shared";

// Companion-page entry — one of the three single-input configs (see
// vite.config.ts). The IIFE wrapper makes the bundle a classic
// script; with a single input there is no shared chunk for the
// wrapper to choke on. emptyOutDir is false: the background config
// (run first) already emptied dist/.
export default defineConfig({
  plugins: [iifeWrapper(), copyManifest()],
  build: {
    outDir: "dist",
    emptyOutDir: false,
    rollupOptions: {
      input: { companion: "src/companion/runtime.ts" },
      output: { entryFileNames: "[name].js", format: "es" },
    },
  },
});
