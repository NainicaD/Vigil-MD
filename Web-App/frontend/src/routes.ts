import type { EntityKind } from "./api/types";

/**
 * Page addresses. The site is a static export, so there is one device page and the entry is
 * in the query (/device?kind=device&id=dev%3A…): a page per device can't be made at build time.
 * Entity ids contain ":" and spaces, so they are encoded.
 */
export const paths = {
  search: (query?: string) => (query ? `/?${new URLSearchParams({ q: query })}` : "/"),
  device: (kind: EntityKind, entityId: string) => `/device?${new URLSearchParams({ kind, id: entityId })}`,
  /** Only in builds with NEXT_PUBLIC_DEV_TOOLS=true. */
  developer: () => "/developer",
};
