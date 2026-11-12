import { defineConfig } from "vite";
import { cpSync } from "node:fs";

export default defineConfig({
  plugins: [{
    name: "copy-extension-static",
    apply: "build",
    closeBundle() {
      cpSync("manifest.json", "dist/manifest.json");
    },
  }],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: { background: "src/background.ts", content: "lib/content.ts", companion: "src/companion/runtime.ts" },
      output: { entryFileNames: "[name].js", format: "es" },
    },
  },
});
