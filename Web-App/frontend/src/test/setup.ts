// Adds DOM matchers such as toBeInTheDocument() and their TypeScript types to Vitest.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { createElement, type AnchorHTMLAttributes } from "react";
import { afterEach, vi } from "vitest";

// The Next.js router does not run in unit tests. These stand-ins read and change jsdom's
// address, so a test sets the page with window.history.replaceState(null, "", "/device?…").
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => window.location.pathname,
  useRouter: () => ({
    push: (url: string) => window.history.pushState(null, "", url),
    replace: (url: string) => window.history.replaceState(null, "", url),
    back: () => window.history.back(),
    forward: () => window.history.forward(),
    refresh: () => {},
    prefetch: () => {},
  }),
}));

// next/link as a plain <a href>: tests check where a link goes, not client-side navigation.
vi.mock("next/link", () => ({
  default: ({ href, prefetch: _prefetch, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; prefetch?: boolean }) =>
    createElement("a", { href, ...rest }),
}));

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});
