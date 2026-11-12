/**
 * Which of an answer's sections to show (handover §5): all, in the order received, except
 * that "refused" shows the refusal text only. With no sections, the answer's text is shown.
 *
 * Rebuilt on 2026-11-10 (see safeText.tsx); the original author should compare it.
 */
import type { AnswerResponse, AnswerSection } from "../api/types";

export function sectionsToShow(r: AnswerResponse): AnswerSection[] {
  const sections = r.sections ?? [];
  if (r.status === "refused") {
    const refusal = sections.filter((s) => s.kind === "medical_refusal");
    return refusal.length > 0 ? refusal.slice(0, 1) : fallback(r);
  }
  return sections.length > 0 ? sections : fallback(r);
}

function fallback(r: AnswerResponse): AnswerSection[] {
  return r.text ? [{ kind: r.status, text: r.text }] : [];
}
