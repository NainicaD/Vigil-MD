import type { EntityKind } from "./api/types";

/** Page addresses. Entity ids contain ":" and spaces, so they are encoded. */
export const paths = {
  search: (query?: string) => (query ? `/?q=${encodeURIComponent(query)}` : "/"),
  device: (kind: EntityKind, entityId: string) =>
    `/device/${encodeURIComponent(kind)}/${encodeURIComponent(entityId)}`,
};
