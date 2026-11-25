import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FilterBar } from "./FilterBar";

const search = vi.fn();
vi.mock("../../api/client", () => ({ api: { search: (...args: unknown[]) => search(...args) } }));

/** Shows the current address, so a test can read what the filter bar wrote. */
function Address() {
  const { search: s } = useLocation();
  return <output data-testid="address">{s}</output>;
}
const address = () => new URLSearchParams(screen.getByTestId("address").textContent ?? "");

function renderBar(url: string, scope: "recalls" | "entity" = "recalls") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <FilterBar scope={scope} />
      <Address />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  search.mockReset();
});

describe("FilterBar: the address bar holds the filters", () => {
  it("reads a shared link back: the filters are there", () => {
    renderBar("/recalls?category=software&class=I&status=open&year_from=2020&year_to=2025&text=battery&product_code=FRN");
    const category = screen.getByRole("group", { name: "Recall category" });
    expect(within(category).getByRole("checkbox", { name: "Software" })).toBeChecked();
    expect(within(category).getByRole("checkbox", { name: "Design" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Class I" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Open (ongoing)" })).toBeChecked();
    expect(screen.getByLabelText("From year")).toHaveValue("2020");
    expect(screen.getByLabelText("To year")).toHaveValue("2025");
    expect(screen.getByLabelText("Text in product, firm or reason")).toHaveValue("battery");
    expect(screen.getByLabelText("Product code")).toHaveValue("FRN");
  });

  it("writes each change to the address and goes back to page 1", async () => {
    renderBar("/recalls?page=3");
    await userEvent.click(screen.getByRole("checkbox", { name: "Software" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Class II" }));
    await userEvent.selectOptions(screen.getByLabelText("From year"), "2021");
    expect(address().getAll("category")).toEqual(["software"]);
    expect(address().getAll("class")).toEqual(["II"]);
    expect(address().get("year_from")).toBe("2021");
    expect(address().get("page")).toBeNull();
    await userEvent.click(screen.getByRole("checkbox", { name: "Software" }));     // off again
    expect(address().getAll("category")).toEqual([]);
  });

  it("applies text after typing stops, and never a single letter", async () => {
    renderBar("/recalls");
    const input = screen.getByLabelText("Text in product, firm or reason");
    await userEvent.type(input, "b");
    await new Promise((r) => setTimeout(r, 500));
    expect(address().get("text")).toBeNull();
    await userEvent.type(input, "attery");
    await waitFor(() => expect(address().get("text")).toBe("battery"));
  });

  it("applies the product code once it has 3 letters", async () => {
    renderBar("/recalls");
    const code = screen.getByLabelText("Product code");
    await userEvent.type(code, "fr");
    expect(address().get("product_code")).toBeNull();
    await userEvent.type(code, "n");
    expect(code).toHaveValue("FRN");
    expect(address().get("product_code")).toBe("FRN");
  });

  it("Clear filters resets to no filters", async () => {
    renderBar("/recalls?category=design&class=III&text=pump&product_code=FRN&manufacturer=acme%20medical");
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(address().toString()).toBe("");
    expect(screen.getByLabelText("Text in product, firm or reason")).toHaveValue("");
    await new Promise((r) => setTimeout(r, 500));                    // the delayed text stays cleared
    expect(address().get("text")).toBeNull();
  });

  it("picks a manufacturer from the search box's manufacturer hits", async () => {
    search.mockResolvedValue({
      query: "acme",
      hits: [
        { id: "device|dev:1", kind: "device", entity_id: "dev:1", title: "Acme Pump", subtitle: null, reports: 1, deaths: 0, injuries: 0, product_codes: [] },
        { id: "manufacturer|acme medical", kind: "manufacturer", entity_id: "acme medical", title: "ACME MEDICAL", subtitle: null, reports: 9, deaths: 0, injuries: 0, product_codes: [] },
      ],
    });
    renderBar("/recalls");
    await userEvent.type(screen.getByLabelText("Manufacturer"), "acme");
    const choice = await screen.findByRole("button", { name: "ACME MEDICAL" });
    expect(screen.queryByRole("button", { name: "Acme Pump" })).toBeNull();          // devices are not offered
    await userEvent.click(choice);
    expect(address().get("manufacturer")).toBe("acme medical");
    await userEvent.click(screen.getByRole("button", { name: "Remove manufacturer acme medical" }));
    expect(address().get("manufacturer")).toBeNull();
  });

  it("offers event types on a device page, and manufacturer and product code only on the recall finder", () => {
    renderBar("/device/device/dev%3A1?event=Death", "entity");
    expect(screen.getByRole("checkbox", { name: "Death" })).toBeChecked();
    expect(screen.queryByLabelText("Product code")).toBeNull();
    expect(screen.queryByLabelText("Manufacturer")).toBeNull();
  });
});
