/**
 * Mock mode (NEXT_PUBLIC_USE_MOCKS=true): the same interface as the real API, answered from the
 * JSON files in this folder. The placeholder files use FICTIONAL devices and the chatbot's
 * own fixed texts; replace them with the real example responses when they arrive.
 *
 *   search.json         GET /api/search (hits are filtered by the words typed)
 *   entities/*.json     GET /api/entity/<kind>/<id>, matched on entity.type + entity.id inside
 *                       each file, so file names do not matter
 *   answers/*.json      POST /api/answer, picked by keywords (MOCK_QUESTIONS below)
 *
 * Small delays imitate the real API so the loading states can be seen.
 */
import type { Api, CallOptions } from "../api/client";
import { NotFoundError, ServiceUnavailableError } from "../api/errors";
import type { AnswerEntity, AnswerResponse, EntityResponse, SearchResponse } from "../api/types";

import ambiguous from "./answers/ambiguous.json";
import answeredCompare from "./answers/answered-compare.json";
import answeredLong from "./answers/answered-long.json";
import answeredNoReports from "./answers/answered-no-reports.json";
import answeredWithRefusal from "./answers/answered-with-refusal.json";
import answered from "./answers/answered.json";
import budget from "./answers/budget.json";
import error from "./answers/error.json";
import help from "./answers/help.json";
import needDevice from "./answers/need_device.json";
import notFound from "./answers/not_found.json";
import refused from "./answers/refused.json";
import scope from "./answers/scope.json";
import unclear from "./answers/unclear.json";
import deviceAcmeInfusion from "./entities/device-acme-infusion.json";
import deviceAcmeInsulin from "./entities/device-acme-insulin.json";
import deviceAcmeLegacy from "./entities/device-acme-legacy.json";
import deviceAcmeair from "./entities/device-acmeair.json";
import manufacturerExampleMedical from "./entities/manufacturer-example-medical.json";
import productCodeZzz from "./entities/product-code-zzz.json";
import searchFile from "./search.json";

// Adding a file: import it above and list it here. Entities are matched on entity.type +
// entity.id inside each file; answers by the names below (MOCK_QUESTIONS uses them).
const SEARCH = searchFile as SearchResponse;

const ENTITIES = new Map<string, EntityResponse>(
  ([deviceAcmeair, deviceAcmeInfusion, deviceAcmeInsulin, deviceAcmeLegacy, manufacturerExampleMedical, productCodeZzz] as EntityResponse[]).map(
    (e) => [`${e.entity.type}|${e.entity.id}`, e],
  ),
);

const ANSWERS = new Map<string, AnswerResponse>(
  Object.entries({
    ambiguous,
    "answered-compare": answeredCompare,
    "answered-long": answeredLong,
    "answered-no-reports": answeredNoReports,
    "answered-with-refusal": answeredWithRefusal,
    answered,
    budget,
    error,
    help,
    need_device: needDevice,
    not_found: notFound,
    refused,
    scope,
    unclear,
  }) as [string, AnswerResponse][],
);

/**
 * Questions to try in mock mode, in the order the keywords are checked. Shown in the README;
 * the first rule whose words all appear in the question wins.
 */
export const MOCK_QUESTIONS: { answer: string; words: string[][]; example: string }[] = [
  { answer: "network failure", words: [["mock offline"]], example: "mock offline" },
  { answer: "error", words: [["mock error"]], example: "mock error" },
  { answer: "budget", words: [["mock budget"]], example: "mock budget" },
  { answer: "help", words: [["help"], ["what can you do"]], example: "What can you do?" },
  { answer: "scope", words: [["poem"], ["weather"], ["joke"]], example: "Write me a poem" },
  {
    answer: "answered-with-refusal",
    words: [["should i", "acmeair"], ["should i", "cpap"]],
    example: "Any deaths for the AcmeAir CPAP? Should I stop using it?",
  },
  { answer: "refused", words: [["should i"], ["is it safe"]], example: "Should I stop using it?" },
  { answer: "answered-compare", words: [[" vs "], ["compare"]], example: "AcmeAir vs Acme Infusion vs nebulizer vs ZZZ" },
  { answer: "ambiguous", words: [["acme pump"]], example: "How many reports for acme pumps?" },
  { answer: "answered-long", words: [["glucosense"], ["insulin"]], example: "What do reports say about the GlucoSense insulin system?" },
  { answer: "answered-no-reports", words: [["infusion"]], example: "Was the Acme Infusion Pump recalled?" },
  { answer: "not_found", words: [["xyz"]], example: "Recalls for xyz" },
  { answer: "answered", words: [["acmeair"], ["acme air"], ["cpap"]], example: "Was the AcmeAir CPAP recalled, and why?" },
  {
    answer: "need_device",
    words: [["recall"], ["death"], ["injur"], ["report"], ["problem"], ["malfunction"]],
    example: "How many deaths were reported?",
  },
];

/** Answer for a question about a known entity (device page or an "ambiguous" choice). */
const ANSWER_FOR_ENTITY: Record<string, string> = {
  "dev:000000acme01": "answered",
  "dev:000000acme02": "answered-no-reports",
  "dev:000000acme03": "answered-long",
};

export function pickMockAnswer(question: string, entity: AnswerEntity | null): string {
  const q = ` ${question.toLowerCase()} `;
  if (q.includes("mock offline")) return "network failure";
  if (entity) {
    const base = ANSWER_FOR_ENTITY[entity.entity_id] ?? "answered";
    return base === "answered" && q.includes("should i") ? "answered-with-refusal" : base;
  }
  for (const rule of MOCK_QUESTIONS) {
    if (rule.words.some((all) => all.every((w) => q.includes(w)))) return rule.answer;
  }
  return "unclear";
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

const copy = <T>(value: T): T => structuredClone(value);

export interface MockDelays {
  search: number;
  entity: number;
  answer: number;
}

export function createMockApi(delays: MockDelays = { search: 200, entity: 300, answer: 1200 }): Api {
  return {
    async search(query, { signal, limit = 10 }: CallOptions & { limit?: number } = {}) {
      await sleep(delays.search, signal);
      const words = query.toLowerCase().split(/\s+/).filter(Boolean);
      const hits = SEARCH.hits.filter((h) => {
        const hay = [h.title, h.subtitle, h.entity_id, ...(h.product_codes ?? [])].join(" ").toLowerCase();
        return words.every((w) => hay.includes(w));
      });
      return { query, hits: copy(hits.slice(0, limit)) };
    },

    async entity(kind, entityId, { signal } = {}) {
      await sleep(delays.entity, signal);
      const found = ENTITIES.get(`${kind}|${entityId}`);
      if (!found) throw new NotFoundError();
      return copy(found);
    },

    async answer(question, entity, { signal } = {}) {
      await sleep(delays.answer, signal);
      const name = pickMockAnswer(question, entity);
      if (name === "network failure") throw new ServiceUnavailableError("network");
      const found = ANSWERS.get(name) ?? ANSWERS.get("unclear");
      if (!found) throw new ServiceUnavailableError("server");
      return copy(found);
    },

    async health({ signal } = {}) {
      await sleep(delays.search, signal);
      return { status: "ok", dataset: "example files (src/mocks/)", rules_fingerprint: null, template_fingerprint: null, index_built: null };
    },
  };
}
