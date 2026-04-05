import path from "path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __REACT_VERSION__: JSON.stringify("19"),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "../src/sovyx/dashboard/static",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes("node_modules/react-dom")) return "vendor-react";
          if (id.includes("node_modules/react/")) return "vendor-react";
          if (id.includes("node_modules/react-router")) return "vendor-router";
          if (id.includes("node_modules/recharts") || id.includes("node_modules/d3-")) return "vendor-recharts";
          if (id.includes("node_modules/i18next") || id.includes("node_modules/react-i18next")) return "vendor-i18n";
          if (id.includes("node_modules/react-hook-form") || id.includes("node_modules/@hookform") || id.includes("node_modules/zod")) return "vendor-forms";
          if (id.includes("node_modules/react-force-graph") || id.includes("node_modules/force-graph") || id.includes("node_modules/d3-force")) return "vendor-graph";
          if (id.includes("node_modules/@base-ui")) return "vendor-ui";
          if (id.includes("node_modules/@tanstack")) return "vendor-tanstack";
        },
      },
    },
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
