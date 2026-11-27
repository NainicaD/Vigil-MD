import { afterEach, describe, expect, it, vi } from "vitest";
import { REVOKE_AFTER_MS, exportPath, fileName, filterParams, hasFilters, saveBlob } from "./export";

describe("export addresses", () => {
  it("keeps only the page's filters, in a fixed order", () => {
    const search = new URLSearchParams("q=pump&page=3&year_to=2025&class=I&class=II&event=Death&manufacturer=acme&text=");
    expect(filterParams(search, "recalls").toString()).toBe("class=I&class=II&year_to=2025&manufacturer=acme");
    expect(filterParams(search, "entity").toString()).toBe("class=I&class=II&year_to=2025&event=Death");
    expect(hasFilters(filterParams(new URLSearchParams("page=2"), "recalls"))).toBe(false);
  });

  it("builds each export address with the format and the filters", () => {
    const f = new URLSearchParams("category=software&class=I");
    expect(exportPath({ kind: "recalls" }, "xlsx", f)).toBe("/export/recalls?format=xlsx&category=software&class=I");
    expect(exportPath({ kind: "entity", entityKind: "device", entityId: "dev:abc 1" }, "pdf", f)).toBe(
      "/export/entity/device/dev%3Aabc%201?format=pdf&category=software&class=I",
    );
    expect(exportPath({ kind: "search", q: "dream station" }, "pdf")).toBe("/export/search?format=pdf&q=dream+station");
    expect(exportPath({ kind: "records", q: "dream station" }, "xlsx")).toBe("/export/records?format=xlsx&q=dream+station");
  });

  it("names the file from fixed parts only, never the visitor's text", () => {
    const day = new Date("2026-11-25T12:00:00Z");
    expect(fileName({ kind: "recalls" }, "xlsx", day)).toBe("vigil-md-recalls-2026-11-25.xlsx");
    expect(fileName({ kind: "search", q: "../../etc" }, "pdf", day)).toBe("vigil-md-search-2026-11-25.pdf");
    expect(fileName({ kind: "records", q: "../../etc" }, "xlsx", day)).toBe("vigil-md-records-2026-11-25.xlsx");
    expect(fileName({ kind: "entity", entityKind: "product_code", entityId: "FRN" }, "pdf", day)).toBe(
      "vigil-md-product-code-2026-11-25.pdf",
    );
  });
});

describe("saveBlob", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("clicks a download link and frees the object URL only after a delay", () => {
    vi.useFakeTimers();
    URL.createObjectURL = vi.fn(() => "blob:file");
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    saveBlob(new Blob(["x"]), "vigil-md-recalls-2026-11-27.xlsx");
    expect(click).toHaveBeenCalledOnce();
    expect(document.querySelector("a[download]")).toBeNull(); // the link is removed again
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(REVOKE_AFTER_MS);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:file");
  });
});
