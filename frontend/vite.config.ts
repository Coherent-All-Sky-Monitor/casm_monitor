import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// casm_monitor frontend build config.
// Output is committed static assets served by the FastAPI app; the server
// never needs node at runtime. See docs/plan.md "Decisions".
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../casm_monitor/web/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8060",
        changeOrigin: true,
      },
      "/ws": {
        target: "http://127.0.0.1:8060",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
