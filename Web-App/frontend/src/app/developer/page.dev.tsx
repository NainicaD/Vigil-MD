import type { Metadata } from "next";
import { DeveloperPage } from "../../views/DeveloperPage";

// /developer. Named page.dev.tsx: next.config.ts only treats ".dev.tsx" files as pages when
// NEXT_PUBLIC_DEV_TOOLS=true, so a public build has no developer page at all (/developer gets
// the normal 404 page) and none of its code.
export const metadata: Metadata = { title: "Developer · Vigil MD", robots: { index: false, follow: false } };

export default function Page() {
  return <DeveloperPage />;
}
