import path from "path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  define: {
    __REACT_VERSION__: JSON.stringify("19"),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
