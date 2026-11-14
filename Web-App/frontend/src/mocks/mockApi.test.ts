import { describe, expect, it } from "vitest";
import { NotFoundError, ServiceUnavailableError } from "../api/errors";
import { MOCK_QUESTIONS, createMockApi, pickMockAnswer } from "./mockApi";

const api = createMockApi({ search: 0, entity: 0, answer: 0 });

describe("mock answers cover every status in the handover (§5)", () => {
  const statuses = ["answered", "refused", "help", "scope", "unclear", "need_device", "not_found", "ambiguous", "budget", "error"];

  it.each(statuses)("%s", async (status) => {
    const example = MOCK_QUESTIONS.find((m) => m.answer === status)?.example ?? "asdf qwerty";
    const res = await api.answer(example, null);
    expect(res.status).toBe(status);
    expect(res.sections.length).toBeGreaterThan(0);
    expect(res.text).toBe(res.sections.map((s) => s.text).join("\n\n"));
  });

  it("every example question gives its listed answer", () => {
    for (const m of MOCK_QUESTIONS) expect(pickMockAnswer(m.example, null), m.example).toBe(m.answer);
  });

  it("refusal comes first when advice and facts are asked together", async () => {
    const res = await api.answer("Any deaths for the AcmeAir CPAP? Should I stop using it?", null);
    expect(res.status).toBe("answered");
    expect(res.sections[0]?.kind).toBe("medical_refusal");
  });

  it("every answered reply ends with the footer", async () => {
    for (const q of ["Was the AcmeAir CPAP recalled?", "glucosense", "Acme Infusion Pump recalls", "AcmeAir vs Acme Infusion"]) {
      const res = await api.answer(q, null);
      expect(res.sections.at(-1)?.kind, q).toBe("footer");
    }
  });

  it("an ambiguous choice re-asked with its entity gets that entity's answer", async () => {
    const amb = await api.answer("How many reports for acme pumps?", null);
    const choice = amb.search!.matches[1]!;
    const res = await api.answer("How many reports for acme pumps?", { entity_type: choice.type, entity_id: choice.id });
    expect(res.entities?.[0]?.id).toBe(choice.id);
  });

  it("'mock offline' simulates a network failure", async () => {
    await expect(api.answer("mock offline", null)).rejects.toBeInstanceOf(ServiceUnavailableError);
  });
});

describe("mock search and entity pages", () => {
  it("search filters by every word typed", async () => {
    const res = await api.search("acme pump");
    expect(res.hits.map((h) => h.entity_id)).toEqual(["dev:000000acme02"]);
  });

  it("each search hit with data opens an entity page; unknown ids are 404", async () => {
    const { hits } = await api.search("");
    for (const h of hits) {
      const page = await api.entity(h.kind, h.entity_id);
      expect(page.entity.id).toBe(h.entity_id);
    }
    await expect(api.entity("device", "dev:unknown")).rejects.toBeInstanceOf(NotFoundError);
  });
});
