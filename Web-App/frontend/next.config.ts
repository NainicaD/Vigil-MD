import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

/**
 * A static site (handover §1): `npm run build` writes plain files to dist/, which the API
 * container (or any static host) serves. No Node server runs in production.
 */
export default function nextConfig(phase: string): NextConfig {
  const dev = phase === PHASE_DEVELOPMENT_SERVER;
  // Development only: forward /api to a locally running API. Never part of the build.
  const proxy = process.env.DEV_API_PROXY?.trim().replace(/\/+$/, "");
  const devTools = process.env.NEXT_PUBLIC_DEV_TOOLS === "true";

  return {
    output: dev ? undefined : "export",
    // Developer-only pages are named page.dev.tsx: they are pages only when the developer tools
    // are on, so a public build doesn't contain them at all.
    pageExtensions: devTools ? ["dev.tsx", "tsx", "ts"] : ["tsx", "ts"],
    distDir: "dist",
    reactStrictMode: true,
    poweredByHeader: false,
    // Always defined at build time, so the bundle holds the value itself and the mock code is
    // dropped when mocks are off. Unset = call /api on the same origin, mocks off.
    env: {
      NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL ?? "",
      NEXT_PUBLIC_USE_MOCKS: process.env.NEXT_PUBLIC_USE_MOCKS ?? "",
      // "true" = build the developer page (/developer) and the call log. Off by default, so a
      // public build contains neither.
      NEXT_PUBLIC_DEV_TOOLS: process.env.NEXT_PUBLIC_DEV_TOOLS ?? "",
      // Shown on the developer page.
      NEXT_PUBLIC_APP_VERSION: process.env.npm_package_version ?? "",
      NEXT_PUBLIC_BUILT_AT: new Date().toISOString(),
    },
    ...(dev && proxy
      ? { rewrites: async () => [{ source: "/api/:path*", destination: `${proxy}/api/:path*` }] }
      : {}),
  };
}
