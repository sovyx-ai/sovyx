import path from "path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "../src/sovyx/dashboard/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://localhost:7777",
      "/ws": {
        target: "ws://localhost:7777",
        ws: true,
      },
    },
  },
});
