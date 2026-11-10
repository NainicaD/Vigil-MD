import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ServiceUnavailableError } from "../../api/errors";
import type { AnswerResponse } from "../../api/types";
import { ChatProvider } from "../../chat/ChatProvider";
import { useChat, type PageEntity } from "../../chat/chatStore";
import ambiguous from "../../mocks/answers/ambiguous.json";
import answeredLong from "../../mocks/answers/answered-long.json";
import withRefusal from "../../mocks/answers/answered-with-refusal.json";
import answered from "../../mocks/answers/answered.json";
import budget from "../../mocks/answers/budget.json";
import error from "../../mocks/answers/error.json";
import help from "../../mocks/answers/help.json";
import needDevice from "../../mocks/answers/need_device.json";
import notFound from "../../mocks/answers/not_found.json";
import refused from "../../mocks/answers/refused.json";
import scope from "../../mocks/answers/scope.json";
import unclear from "../../mocks/answers/unclear.json";
import { ChatPanel } from "./ChatPanel";

const answer = vi.fn();
vi.mock("../../api/client", () => ({ api: { answer: (...args: unknown[]) => answer(...args) } }));

function SetPageEntity({ value }: { value: PageEntity }) {
  const { setPageEntity } = useChat();
  useEffect(() => setPageEntity(value), [setPageEntity, value]);
  return null;
}

function renderPanel(pageEntity?: PageEntity) {
  return render(
    <MemoryRouter>
      <ChatProvider>
        {pageEntity && <SetPageEntity value={pageEntity} />}
        <ChatPanel />
      </ChatProvider>
    </MemoryRouter>,
  );
}

async function ask(question: string) {
  const input = screen.getByLabelText("Your question");
  await userEvent.type(input, `${question}{Enter}`);
}

function lastReply(): HTMLElement {
  const replies = document.querySelectorAll<HTMLElement>(".reply:not(.reply-pending)");
  return replies[replies.length - 1]!;
}

const reply = (r: unknown) => answer.mockResolvedValueOnce(r as AnswerResponse);

beforeEach(() => {
  answer.mockReset();
});

describe("statuses from /api/answer (handover §5)", () => {
  it("answered: all sections in the order received, footer last", async () => {
    reply(answered);
    renderPanel();
    await ask("Was the AcmeAir CPAP recalled?");
    await waitFor(() => expect(lastReply()).toBeTruthy());
    const shown = Array.from(lastReply().querySelectorAll(".section"), (el) =>
      [...el.classList].find((c) => c.startsWith("section-")),
    );
    expect(shown).toEqual(["section-headline", "section-counts", "section-problems", "section-recalls", "section-footer"]);
    expect(lastReply()).toHaveTextContent("Reports without a UDI are not linked to a device");
  });

  it("answered with advice asked: the refusal comes first, unchanged, not styled as an error", async () => {
    reply(withRefusal);
    renderPanel();
    await ask("Any deaths for the AcmeAir CPAP? Should I stop using it?");
    await waitFor(() => expect(lastReply()).toBeTruthy());
    const first = lastReply().querySelector(".section")!;
    expect(first).toHaveClass("refusal-note");
    expect(first.textContent).toBe(withRefusal.sections[0]!.text);
    expect(within(lastReply()).queryByRole("alert")).toBeNull();
  });

  it("refused: the refusal text only", async () => {
    reply({ ...refused, sections: [...refused.sections, { kind: "headline", text: "should not show" }] });
    renderPanel();
    await ask("Should I stop using it?");
    await waitFor(() => expect(lastReply()).toBeTruthy());
    expect(lastReply().querySelectorAll(".section")).toHaveLength(1);
    expect(lastReply()).not.toHaveTextContent("should not show");
  });

  it.each([
    ["help", help],
    ["scope", scope],
    ["unclear", unclear],
    ["budget", budget],
  ])("%s: the fixed text", async (_name, fixture) => {
    reply(fixture);
    renderPanel();
    await ask("a question");
    await waitFor(() => expect(lastReply()).toBeTruthy());
    const last = fixture.sections[fixture.sections.length - 1]!;
    expect(lastReply()).toHaveTextContent(last.text.replace(/\*\*/g, ""));
  });

  it("need_device: the text, and the focus goes back to the input", async () => {
    reply(needDevice);
    renderPanel();
    await ask("How many deaths were reported?");
    await waitFor(() => expect(lastReply()).toHaveTextContent("Which device do you mean?"));
    await waitFor(() => expect(screen.getByLabelText("Your question")).toHaveFocus());
  });

  it("not_found: the text and a link to the search page", async () => {
    reply(notFound);
    renderPanel();
    await ask("Recalls for xyz");
    await waitFor(() => expect(lastReply()).toHaveTextContent("couldn't find"));
    expect(within(lastReply()).getByRole("link", { name: /search the device list/i })).toHaveAttribute("href", "/");
  });

  it("ambiguous: each choice is a button that re-sends the question with that entity", async () => {
    reply(ambiguous);
    reply(answeredLong);
    renderPanel();
    await ask("How many reports for acme pumps?");
    const group = await screen.findByRole("group", { name: /choose the one you mean/i });
    const buttons = within(group).getAllByRole("button");
    expect(buttons).toHaveLength(ambiguous.search.matches.length);
    expect(lastReply()).toHaveTextContent("matches more than one entry"); // whole text shown

    const choice = ambiguous.search.matches[1]!;
    await userEvent.click(within(group).getByRole("button", { name: choice.title }));
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
    expect(answer.mock.calls[1]?.[0]).toBe("How many reports for acme pumps?");
    expect(answer.mock.calls[1]?.[1]).toEqual({ entity_type: choice.type, entity_id: choice.id });
  });

  it("error: the text and a Try again button that asks again", async () => {
    reply(error);
    reply(answered);
    renderPanel();
    await ask("Was the AcmeAir CPAP recalled?");
    await waitFor(() => expect(lastReply()).toHaveTextContent("I can't reach the FDA data right now"));
    await userEvent.click(within(lastReply()).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
    expect(answer.mock.calls[1]).toEqual(answer.mock.calls[0]);
  });

  it("network failure or timeout: the short service message, with Try again", async () => {
    answer.mockImplementationOnce(async () => {
      throw new ServiceUnavailableError("timeout");
    });
    renderPanel();
    await ask("Was the AcmeAir CPAP recalled?");
    expect(await screen.findByText("Can't reach the service, please try again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("chat behaviour (handover §3, §5)", () => {
  it("shows 'Looking this up…' and blocks a second send until the answer arrives", async () => {
    let resolve!: (r: AnswerResponse) => void;
    answer.mockImplementationOnce(() => new Promise<AnswerResponse>((r) => (resolve = r)));
    renderPanel();
    await ask("first question");
    expect(screen.getByText("Looking this up…")).toBeInTheDocument();
    await ask("second question");
    expect(answer).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /wait/i })).toBeDisabled();
    resolve(help as AnswerResponse);
    await waitFor(() => expect(screen.queryByText("Looking this up…")).toBeNull());
  });

  it("sends entity: null when no device page is open", async () => {
    reply(help);
    renderPanel();
    await ask("What can you do?");
    await waitFor(() => expect(answer).toHaveBeenCalled());
    expect(answer.mock.calls[0]?.[1]).toBeNull();
  });

  it("on a device page, sends that device as entity", async () => {
    reply(answered);
    renderPanel({ entity: { entity_type: "device", entity_id: "dev:000000acme01" }, title: "AcmeAir CPAP" });
    expect(await screen.findByText("AcmeAir CPAP")).toBeInTheDocument(); // "Questions here are about…"
    await ask("Any recalls?");
    await waitFor(() => expect(answer).toHaveBeenCalled());
    expect(answer.mock.calls[0]?.[1]).toEqual({ entity_type: "device", entity_id: "dev:000000acme01" });
  });

  it("report excerpts with <script> show as text", async () => {
    reply(answeredLong);
    renderPanel();
    await ask("What do reports say about the GlucoSense insulin system?");
    await waitFor(() => expect(lastReply()).toHaveTextContent("<script>alert('x')</script>"));
    expect(lastReply().querySelector("script, img, a[href^='https://example.org']")).toBeNull();
  });
});
