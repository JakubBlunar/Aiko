/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import path from "node:path";

// The Live2D engine and channels are plain TypeScript: no DOM, no Pixi.
// Channels accept fake adapters / event sources / clocks via constructor
// injection so we can run them under the Node test environment with no
// jsdom overhead. The Live2DAvatar React component itself is intentionally
// not under test — it shrinks to mount/dispose plumbing in Phase 11 and
// is covered by manual smoke confirmation.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    // Surface uncovered console.error/warn during tests instead of swallowing.
    onConsoleLog(log, type) {
      if (type === "stderr") {
        return true;
      }
      return undefined;
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
