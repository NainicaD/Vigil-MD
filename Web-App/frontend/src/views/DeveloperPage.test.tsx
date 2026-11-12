import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ServiceUnavailableError } from "../api/errors";
import type { AnswerDetails } from "../api/types";
import { clearCalls, recordCall } from "../dev/callLog";
import { DeveloperPage } from "./DeveloperPage";

const health = vi.fn();
const answer = vi.fn();
vi.mock("../api/client", () => ({
  api: {
    health: (...args: unknown[]) => health(...args),
    answer: (...args: unknown[]) => answer(...args),
  },
}));

const DETAILS: AnswerDetails = {
  status: "answered",
  text: "**HeartMate 3**: 21,203 reports.",
  sections: [
    { kind: "headline", text: "**HeartMate 3**: 21,203 reports." },
    { kind: "footer", text: "A report does not prove that the device caused the event." },
  ],
  labels: ["counts"],
  rule_hits: ["should_i"],
  run_queries: ["counts", "problems"],
  reply_kind: "medical_refusal",
  search: {
    status: "resolved",
    phrase: "heartmate 3",
    matches: [{ type: "device", id: "dev:abc", title: "Thoratec HeartMate 3", score: 0.97 }],
  },
  entities: [{ type: "device", id: "dev:abc", title: "Thoratec HeartMate 3", found: true, total_reports: 21203 }],
  sources: [{ query: "counts", tables: ["entity_counts"] }],
  data_as_of: { entity_counts: "2026-11-08T03:00:00+00:00" },
  budget_gb: { max: 0.5, spent: 0.042 },
  template_fingerprint: "tmpl123",
  rules_fingerprint: "rules456",
};

beforeEach(() => {
  health.mockReset();
  answer.mockReset();
  clearCalls();
  health.mockResolvedValue({
    status: "ok",
    dataset: "vigil_md_dev",
    rules_fingerprint: "rules456",
    template_fingerprint: "tmpl123",
    index_built: "2026-11-10T12:00:00",
  });
});

describe("DeveloperPage: service status", () => {
  it("shows the API's status, dataset and versions, and this front end's settings", async () => {
    render(<DeveloperPage />);
    const status = screen.getByRole("region", { name: "Service status" });
    await waitFor(() => expect(status).toHaveTextContent("vigil_md_dev"));
    expect(status).toHaveTextContent("Statusok");
    expect(status).toHaveTextContent("rules456");
    expect(status).toHaveTextContent("tmpl123");
    expect(status).toHaveTextContent("API address/api");
    expect(status).toHaveTextContent("Example dataoff");
  });

  it("says when the API can't be reached, and checks again on request", async () => {
    health.mockImplementationOnce(async () => {
      throw new ServiceUnavailableError("network");
    });
    render(<DeveloperPage />);
    expect(await screen.findByText(/Not reachable: Can't reach the service/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(health).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("ok")).toBeInTheDocument();
  });
});

describe("DeveloperPage: answer inspector", () => {
  it("shows how the answer was made: router, search, entries, queries, data dates, cost, raw JSON", async () => {
    answer.mockResolvedValue(DETAILS);
    render(<DeveloperPage />);
    const inspector = screen.getByRole("region", { name: "Answer inspector" });
    await userEvent.type(within(inspector).getByLabelText("Question"), "How many deaths for the HeartMate 3?");
    await userEvent.click(within(inspector).getByRole("button", { name: "Ask" }));

    await within(inspector).findByText("As the user sees it");
    expect(answer).toHaveBeenCalledWith("How many deaths for the HeartMate 3?", null);
    expect(inspector).toHaveTextContent("0.0420 GB of 0.5000 GB allowed");
    expect(within(inspector).getByText("should_i")).toHaveClass("chip");
    expect(within(inspector).getAllByText("counts").length).toBeGreaterThanOrEqual(2); // label and query
    expect(inspector).toHaveTextContent("medical_refusal");
    expect(inspector).toHaveTextContent('"heartmate 3"');
    expect(within(inspector).getAllByText("Thoratec HeartMate 3").length).toBe(2); // match and entry
    expect(inspector).toHaveTextContent("0.97");
    expect(inspector).toHaveTextContent("entity_counts");
    expect(inspector).toHaveTextContent("tmpl123");
    // the reply as the assistant shows it, rendered safely
    expect(within(inspector).getByText("HeartMate 3", { selector: "strong" })).toBeInTheDocument();
    // raw JSON as text
    expect(inspector.querySelector(".dev-json")?.textContent).toContain('"rule_hits"');
  });

  it("sends the question about an entry when one is given", async () => {
    answer.mockResolvedValue({ status: "answered", text: "", sections: [] });
    render(<DeveloperPage />);
    const inspector = screen.getByRole("region", { name: "Answer inspector" });
    await userEvent.type(within(inspector).getByLabelText("Question"), "Any recalls?");
    await userEvent.selectOptions(within(inspector).getByLabelText(/about entry/i), "device");
    await userEvent.type(within(inspector).getByLabelText("Entry id"), "dev:000000acme01");
    await userEvent.click(within(inspector).getByRole("button", { name: "Ask" }));
    await waitFor(() =>
      expect(answer).toHaveBeenCalledWith("Any recalls?", { entity_type: "device", entity_id: "dev:000000acme01" }),
    );
    expect(await within(inspector).findByText(/Skipped: the question was sent about device/)).toBeInTheDocument();
    // fields an answer doesn't carry are named as missing, not shown as empty
    expect(within(inspector).getAllByText("not in this response").length).toBeGreaterThan(0);
  });

  it("shows the API's message when the question fails", async () => {
    answer.mockImplementation(async () => {
      throw new ServiceUnavailableError("timeout");
    });
    render(<DeveloperPage />);
    const inspector = screen.getByRole("region", { name: "Answer inspector" });
    await userEvent.type(within(inspector).getByLabelText("Question"), "q");
    await userEvent.click(within(inspector).getByRole("button", { name: "Ask" }));
    expect(await within(inspector).findByRole("alert")).toHaveTextContent("Can't reach the service, please try again.");
  });
});

describe("DeveloperPage: session log", () => {
  it("lists this tab's calls, newest first, with a summary, and clears", async () => {
    recordCall({ at: Date.now(), call: "search", detail: '"acme"', outcome: "ok", result: "6 hits", ms: 210, gb: null });
    recordCall({ at: Date.now(), call: "answer", detail: '"Any recalls?"', outcome: "failed", result: "HTTP 429 rate_limited", ms: 40, gb: null });
    render(<DeveloperPage />);
    const log = screen.getByRole("region", { name: "Session log" });
    await waitFor(() => expect(within(log).getAllByRole("row").length).toBeGreaterThanOrEqual(3));
    const rows = within(log).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("HTTP 429 rate_limited"); // newest first (after the header row)
    expect(log).toHaveTextContent("Failed1");
    await userEvent.click(within(log).getByRole("button", { name: "Clear log" }));
    expect(within(log).getByText(/No calls yet/)).toBeInTheDocument();
  });
});
