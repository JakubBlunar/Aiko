import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
var BACKEND = "http://127.0.0.1:6275";
export default defineConfig({
    plugins: [react()],
    resolve: {
        alias: {
            "@": path.resolve(__dirname, "./src"),
        },
    },
    server: {
        port: 5173,
        proxy: {
            "/api": { target: BACKEND, changeOrigin: true },
            "/persona": { target: BACKEND, changeOrigin: true },
            "/ws": { target: BACKEND.replace("http", "ws"), ws: true },
        },
    },
    build: {
        outDir: "dist",
        emptyOutDir: true,
    },
});
