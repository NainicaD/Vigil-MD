import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SearchHit } from "../api/types";
import { NO_RESULTS_MESSAGE } from "../components/SearchResults";
import { SearchPage } from "./SearchPage";

const search = vi.fn();
vi.mock("../api/client", () => ({ api: { search: (...args: unknown[]) => search(...args) } }));

const hit = (kind: SearchHit["kind"], entity_id: string, title: string, reports: number | null): SearchHit => ({
  id: `${kind}|${entity_id}`, kind, entity_id, title, subtitle: kind === "device" ? "Example Co." : null,
  reports, deaths: 9, injuries: 9, product_codes: ["ZZZ"],
});

function renderPage() {
  return render(
    <MemoryRouter>
      <SearchPage />
    </MemoryRouter>,
  );
}

// Braces matter: a function returned from beforeEach is run by Vitest as cleanup.
beforeEach(() => {
  search.mockReset();
});

describe("SearchPage (handover §3)", () => {
  it("does not search below 2 characters", async () => {
    renderPage();
    await userEvent.type(screen.getByLabelText(/device, company or product code/i), "a");
    expect(await screen.findByText(/at least 2 characters/i)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 400));
    expect(search).not.toHaveBeenCalled();
  });

  it("searches once after typing stops and groups results by kind", async () => {
    search.mockResolvedValue({
      query: "acme",
      hits: [hit("device", "dev:1", "Acme Pump", 1234), hit("manufacturer", "acme co", "Acme Co.", 1), hit("product_code", "ZZZ", "Example code", null)],
    });
    renderPage();
    await userEvent.type(screen.getByLabelText(/device, company or product code/i), "acme");
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
    expect(search.mock.calls[0]?.[0]).toBe("acme");

    const devices = await screen.findByRole("region", { name: /devices/i });
    const link = within(devices).getByRole("link", { name: /acme pump/i });
    expect(link).toHaveAttribute("href", "/device/device/dev%3A1");
    expect(link).toHaveTextContent("1,234 reports");
    expect(link).toHaveTextContent("Example Co.");
    expect(link).not.toHaveTextContent(/death|injur/i); // only kind, title, company, reports
    expect(screen.getByRole("region", { name: /companies/i })).toHaveTextContent("1 report");
    expect(screen.getByRole("link", { name: /acme co\./i })).toHaveAttribute("href", "/device/manufacturer/acme%20co");
    expect(screen.getByRole("region", { name: /product codes/i })).toBeInTheDocument();
  });

  it("shows the handover's text when nothing is found", async () => {
    search.mockResolvedValue({ query: "zzzz", hits: [] });
    renderPage();
    await userEvent.type(screen.getByLabelText(/device, company or product code/i), "zzzz");
    expect(await screen.findByText(NO_RESULTS_MESSAGE)).toBeInTheDocument();
    expect(NO_RESULTS_MESSAGE).toBe("Nothing found. Try the brand name, the company or the 3-letter product code.");
  });

  it("shows the service message when the API fails", async () => {
    search.mockImplementation(async () => {
      throw new Error("down");
    });
    renderPage();
    await userEvent.type(screen.getByLabelText(/device, company or product code/i), "acme");
    expect(await screen.findByText("Can't reach the service, please try again.")).toBeInTheDocument();
  });
});
