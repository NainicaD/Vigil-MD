import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiMessageError } from "../../api/errors";
import { DownloadButtons } from "./DownloadButtons";

const download = vi.fn();
vi.mock("../../api/client", () => ({ api: { download: (...args: unknown[]) => download(...args) } }));

beforeEach(() => {
  download.mockReset();
  // jsdom has no object URLs; the save step only needs them to exist.
  URL.createObjectURL = vi.fn(() => "blob:test");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => vi.restoreAllMocks());

describe("DownloadButtons", () => {
  it("calls the right address with the current filters and saves the file", async () => {
    download.mockResolvedValue(new Blob(["x"]));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<DownloadButtons target={{ kind: "recalls" }} filters={new URLSearchParams("category=software&class=I")} count={1284} />);
    expect(screen.getByText("1,284 recalls match")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Download Excel" }));
    await waitFor(() => expect(download).toHaveBeenCalledWith("/export/recalls?format=xlsx&category=software&class=I"));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });

  it("is off while the file is being made, with 'Preparing file…'", async () => {
    let finish!: (b: Blob) => void;
    download.mockImplementation(() => new Promise<Blob>((r) => (finish = r)));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<DownloadButtons target={{ kind: "search", q: "pump" }} noun="results" />);
    await userEvent.click(screen.getByRole("button", { name: "Download PDF" }));
    expect(screen.getByRole("status")).toHaveTextContent("Preparing file…");
    expect(screen.getByRole("button", { name: "Download PDF" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Download Excel" })).toBeDisabled();
    finish(new Blob(["x"]));
    await waitFor(() => expect(screen.getByRole("button", { name: "Download PDF" })).toBeEnabled());
    expect(download).toHaveBeenCalledWith("/export/search?format=pdf&q=pump");
  });

  it("shows the API's error text (e.g. too many rows)", async () => {
    download.mockRejectedValue(new ApiMessageError(413, "too_many", "2,400 recalls match. Narrow the filters: this file holds at most 1,000."));
    render(<DownloadButtons target={{ kind: "recalls" }} count={900} />);
    await userEvent.click(screen.getByRole("button", { name: "Download PDF" }));
    expect(await screen.findByText(/Narrow the filters: this file holds at most 1,000/)).toBeInTheDocument();
  });

  it("turns the PDF off over its limit and says why; Excel stays available", () => {
    render(<DownloadButtons target={{ kind: "recalls" }} count={1284} />);
    expect(screen.getByRole("button", { name: "Download PDF" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Download Excel" })).toBeEnabled();
    expect(screen.getByText(/A PDF holds at most 1,000 recalls/)).toBeInTheDocument();
  });

  it("has nothing to download when nothing matches", () => {
    render(<DownloadButtons target={{ kind: "recalls" }} count={0} />);
    expect(screen.getByRole("button", { name: "Download PDF" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Download Excel" })).toBeDisabled();
  });
});
