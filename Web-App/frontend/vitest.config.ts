import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Tests only (Next.js builds the site). jsdom, with next/navigation and next/link stood in for
// by src/test/setup.ts.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
