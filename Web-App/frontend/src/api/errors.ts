/** The handover's wording for a network failure or no answer within 30 s (§5). */
export const SERVICE_UNAVAILABLE_MESSAGE = "Can't reach the service, please try again.";

/** Network failure, timeout or a server error: show SERVICE_UNAVAILABLE_MESSAGE. */
export class ServiceUnavailableError extends Error {
  constructor(public readonly reason: "network" | "timeout" | "server", cause?: unknown) {
    super(SERVICE_UNAVAILABLE_MESSAGE, { cause });
    this.name = "ServiceUnavailableError";
  }
}

/** The API answered 404: no such entry (e.g. a device link that no longer exists). */
export class NotFoundError extends Error {
  constructor() {
    super("Not found.");
    this.name = "NotFoundError";
  }
}

/** The request was cancelled by the app (e.g. a newer search replaced it). Ignore it. */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
