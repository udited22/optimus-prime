import { defineConfig } from "vite";

// The Python server (python -m project100c.observability.dashboard) serves dist/ and the /api routes.
// `npm run dev` proxies /api to it so the HUD can be iterated on with hot reload.
export default defineConfig({
  base: "/",
  build: { outDir: "dist", assetsDir: "assets", chunkSizeWarningLimit: 1200, sourcemap: false },
  server: { port: 5173, proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: false } } },
});
