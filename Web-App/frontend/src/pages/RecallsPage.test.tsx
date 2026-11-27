import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MOCK_RECALLS, mockRecallPage } from "../mocks/mockRecalls";
import { RecallsPage } from "./RecallsPage";

const recalls = vi.fn();
vi.mock("../api/client", () => ({
  api: { recalls: (...args: unknown[]) => recalls(...args), search: vi.fn(), download: vi.fn() },
}));

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <RecallsPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  recalls.mockReset();
  recalls.mockImplementation(async (filters: URLSearchParams, page: number) => mockRecallPage(filters, page));
});

describe("RecallsPage (recall finder)", () => {
  it("asks the API with the address's filters and shows the count, the table and the pages", async () => {
    renderAt("/recalls?class=I&page=1");
    expect(await screen.findByText(/recalls match/)).toBeInTheDocument();
    const [filters, page] = recalls.mock.calls[0]!;
    expect((filters as URLSearchParams).toString()).toBe("class=I");
    expect(page).toBe(1);
    const expected = MOCK_RECALLS.filter((r) => r.recall_class === "Class I").length;
    expect(screen.getByText(`${expected} recalls match`)).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(expected + 1);
  });

  it("pages through 50 at a time", async () => {
    renderAt("/recalls");
    expect(await screen.findByText(`Page 1 of 2 (${MOCK_RECALLS.length} recalls)`)).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(51);
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(recalls).toHaveBeenLastCalledWith(expect.any(URLSearchParams), 2, expect.anything()));
    expect(await screen.findByText(`Page 2 of 2 (${MOCK_RECALLS.length} recalls)`)).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(MOCK_RECALLS.length - 50 + 1);
  });

  it("a filter change reloads with the new filters, from page 1", async () => {
    renderAt("/recalls?page=2");
    await screen.findByText(/Page 2 of 2/);
    await userEvent.click(screen.getByRole("checkbox", { name: "Software" }));
    await waitFor(() => {
      const [filters, page] = recalls.mock.calls.at(-1)!;
      expect((filters as URLSearchParams).toString()).toBe("category=software");
      expect(page).toBe(1);
    });
  });

  it("shows the API's error and Try again", async () => {
    recalls.mockReset();
    recalls.mockRejectedValueOnce(new Error("down"));
    recalls.mockImplementation(async (f: URLSearchParams, p: number) => mockRecallPage(f, p));
    renderAt("/recalls");
    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/recalls match/)).toBeInTheDocument();
  });
});
