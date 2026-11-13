import { defineConfig } from "vite";
import { cpSync } from "node:fs";

export default defineConfig({
  plugins: [{
    name: "copy-extension-static",
    apply: "build",
    renderChunk(code, chunk) {
      if (chunk.fileName === "content.js" || chunk.fileName === "companion.js") {
        return `(() => {\n${code}\n})();`;
      }
      return null;
    },
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
