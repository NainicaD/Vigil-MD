/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // Loads .env files for this config only. DEV_API_PROXY (no VITE_ prefix) never reaches the
  // browser bundle; it only sets up the dev server's proxy.
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.DEV_API_PROXY?.trim();

  return {
    plugins: [react()],
    // Absolute asset paths (/assets/...): the API serves the site from its root, and pages
    // such as /device/... must still find the assets when opened or reloaded directly.
    base: "/",
    server: {
      port: 5173,
      proxy: proxyTarget ? { "/api": { target: proxyTarget, changeOrigin: true } } : undefined,
    },
    build: {
      outDir: "dist",
      sourcemap: false,
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: false,
    },
  };
});
