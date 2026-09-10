import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built app is committed under oxide_triage/ui/dist and served by FastAPI, so a Python
// install needs no Node. `npm run dev` proxies /api to a running `oxide-triage serve`.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../oxide_triage/ui/dist", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } } },
});
