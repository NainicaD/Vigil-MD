import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiMessageError, NotFoundError, ServiceUnavailableError } from "../api/errors";
import { ChatProvider } from "../chat/ChatProvider";
import type { EntityResponse } from "../api/types";
import acmeair from "../mocks/entities/device-acmeair.json";
import infusion from "../mocks/entities/device-acme-infusion.json";
import insulin from "../mocks/entities/device-acme-insulin.json";
import legacy from "../mocks/entities/device-acme-legacy.json";
import { DevicePage, NO_DATA_MESSAGE } from "./DevicePage";

const entity = vi.fn();
vi.mock("../api/client", () => ({ api: { entity: (...args: unknown[]) => entity(...args) } }));

function renderAt(path: string) {
  window.history.replaceState(null, "", path);
  return render(
    <ChatProvider>
      <DevicePage />
    </ChatProvider>,
  );
}

beforeEach(() => {
  entity.mockReset();
});

describe("DevicePage (handover §3, §4)", () => {
  it("shows name, company, codes, counts with the note beside them, problems, recalls and sources", async () => {
    entity.mockResolvedValue(acmeair as EntityResponse);
    renderAt("/device?kind=device&id=dev%3A000000acme01");

    expect(await screen.findByRole("heading", { level: 1, name: "AcmeAir CPAP" })).toBeInTheDocument();
    expect(entity).toHaveBeenCalledWith("device", "dev:000000acme01", expect.anything());
    expect(screen.getByText("Example Medical Inc.", { selector: ".entity-company" })).toBeInTheDocument();
    expect(screen.getByText("ZZZ")).toBeInTheDocument();

    const reports = screen.getByRole("region", { name: "Reports" });
    expect(reports).toHaveTextContent("1,234 reports");
    expect(within(reports).getByText(acmeair.counts_note)).toBeInTheDocument(); // next to the numbers
    expect(reports).toHaveTextContent("Malfunction1,111 reports");

    expect(screen.getByRole("region", { name: "Reports per year" })).toHaveTextContent("2021");
    const problems = screen.getByRole("region", { name: "Reported problems" });
    expect(problems).toHaveTextContent("Example Problem A");
    expect(problems).toHaveTextContent("No patient harm reported: 900 reports");

    const recalls = within(screen.getByRole("region", { name: "Recalls" })).getAllByRole("listitem");
    expect(recalls[0]).toHaveTextContent("2024-05-02"); // API order kept: newest first
    expect(recalls[1]).toHaveTextContent("2022-09-14");
    expect(recalls[0]).toHaveTextContent("Class II");

    expect(screen.getByRole("region", { name: "Data sources" })).toHaveTextContent("Data as of: 2026-11-08");
  });

  it("never calls counts failures or rates", async () => {
    entity.mockResolvedValue(insulin as EntityResponse);
    const { container } = renderAt("/device?kind=device&id=dev%3A000000acme03");
    await screen.findByRole("region", { name: "Reports" });
    // Whole words only: quoted FDA text may contain e.g. "separate".
    expect(container.textContent).not.toMatch(/\b(failures?|failure rates?|rates?|safe|unsafe|dangerous?)\b/i);
  });

  it("handles missing recall fields and many recalls", async () => {
    entity.mockResolvedValue(insulin as EntityResponse);
    renderAt("/device?kind=device&id=dev%3A000000acme03");
    const recalls = await screen.findByRole("region", { name: "Recalls" });
    expect(recalls).toHaveTextContent("57 recalls found. Showing the newest 3.");
    expect(within(recalls).getAllByText("Not given").length).toBeGreaterThanOrEqual(3);
  });

  it("an entry with no data (0 reports, no recalls) still renders", async () => {
    entity.mockResolvedValue(infusion as EntityResponse);
    renderAt("/device?kind=device&id=dev%3A000000acme02");
    expect(await screen.findByRole("region", { name: "Reports" })).toHaveTextContent("0 reports");
    expect(screen.getByRole("region", { name: "Recalls" })).toHaveTextContent("No recalls found.");
    expect(screen.queryByRole("region", { name: "Reports per year" })).toBeNull();
  });

  it("found: false shows that there is no data", async () => {
    entity.mockResolvedValue(legacy as EntityResponse);
    renderAt("/device?kind=device&id=dev%3A000000acme04");
    expect(await screen.findByText(NO_DATA_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Reports" })).toBeNull();
  });

  it("404 shows page not found", async () => {
    entity.mockImplementation(async () => {
      throw new NotFoundError();
    });
    renderAt("/device?kind=device&id=dev%3Agone");
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("a failed load offers Try again, which loads again", async () => {
    entity.mockImplementationOnce(async () => {
      throw new ServiceUnavailableError("network");
    });
    entity.mockResolvedValue(acmeair as EntityResponse);
    renderAt("/device?kind=device&id=dev%3A000000acme01");
    expect(await screen.findByText("Can't reach the service, please try again.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(entity).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("heading", { level: 1, name: "AcmeAir CPAP" })).toBeInTheDocument();
  });

  it("an API error with its own text (e.g. budget) shows that text", async () => {
    const text = "That question needs more data than I can read for one question.";
    entity.mockImplementation(async () => {
      throw new ApiMessageError(413, "budget", text);
    });
    renderAt("/device?kind=device&id=dev%3A000000acme01");
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("an unknown kind in the address is page not found, without calling the API", () => {
    renderAt("/device?kind=gadget&id=x");
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(entity).not.toHaveBeenCalled();
  });
});
