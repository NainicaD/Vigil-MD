/**
 * The draft API contract (front-end handover, section 2). Field names are fixed by the
 * contract; do not rename them. Real data can be long or missing, so fields the handover
 * says may be absent are typed as nullable and the UI must handle both.
 */

export type EntityKind = "device" | "manufacturer" | "product_code";

export const ENTITY_KINDS: readonly EntityKind[] = ["device", "manufacturer", "product_code"];

export function isEntityKind(value: string | undefined | null): value is EntityKind {
  return !!value && (ENTITY_KINDS as readonly string[]).includes(value);
}

// ---------------------------------------------------------------- GET /api/search

export interface SearchHit {
  /** "<kind>|<entity_id>", unique per hit. */
  id: string;
  kind: EntityKind;
  entity_id: string;
  title: string;
  /** The company, for a device. May be missing. */
  subtitle: string | null;
  reports: number | null;
  deaths: number | null;
  injuries: number | null;
  product_codes: string[] | null;
}

export interface SearchResponse {
  query: string;
  hits: SearchHit[];
}

// ---------------------------------------------------------------- GET /api/entity/<kind>/<id>

export interface EntityInfo {
  type: EntityKind;
  id: string;
  /** false: the entry is in the search index but has no data. */
  found: boolean;
  title?: string | null;
  subtitle?: string | null;
  product_codes?: string[] | null;
}

export interface Counts {
  total_reports: number;
  /** e.g. {"Malfunction": 29000, "Injury": 888, "Death": 468} */
  by_event_type: Record<string, number>;
  /** year -> event type -> reports, e.g. {"2021": {"Malfunction": 9000}} */
  by_year: Record<string, Record<string, number>>;
}

export interface ProblemTerm {
  term: string;
  reports: number;
}

export interface Problems {
  device: ProblemTerm[];
  patient: ProblemTerm[];
  no_harm_reports: number | null;
}

export interface Recall {
  recall_number: string | null;
  recall_class: string | null;
  record_date: string | null;
  recall_status: string | null;
  product: string | null;
  firm: string | null;
  reason_text: string | null;
  root_cause: string | null;
  action: string | null;
}

export interface Recalls {
  total: number;
  /** Newest first, as sent by the API. */
  shown: Recall[];
}

export interface Source {
  query: string;
  tables: string[];
}

export interface EntityResponse {
  entity: EntityInfo;
  counts: Counts;
  /** Show as given, next to the numbers. */
  counts_note: string | null;
  problems: Problems;
  recalls: Recalls;
  /** table -> ISO timestamp of its last update */
  data_as_of: Record<string, string | null>;
  sources: Source[];
}

// ---------------------------------------------------------------- POST /api/answer

/** The entity sent with a question (device page, or an "ambiguous" choice). */
export interface AnswerEntity {
  entity_type: EntityKind;
  entity_id: string;
}

/** The API rejects longer questions (api/app.py MAX_QUESTION_CHARS). */
export const MAX_QUESTION_LENGTH = 1000;

export interface AnswerRequest {
  question: string;
  entity: AnswerEntity | null;
}

export type AnswerStatus =
  | "answered"
  | "refused"
  | "help"
  | "scope"
  | "unclear"
  | "need_device"
  | "not_found"
  | "ambiguous"
  | "budget"
  | "error";

export type SectionKind =
  | "medical_refusal"
  | "help"
  | "scope"
  | "unclear"
  | "need_device"
  | "not_found"
  | "ambiguous"
  | "note"
  | "headline"
  | "identity"
  | "counts"
  | "problems"
  | "recalls"
  | "narratives"
  | "budget"
  | "error"
  | "footer";

export interface AnswerSection {
  /** One of SectionKind; typed as string so an unknown new kind still renders. */
  kind: SectionKind | (string & {});
  /** Markdown subset: **bold**, *italic*, "- " list lines. Render with SafeText. */
  text: string;
  entity?: string | null;
}

export interface SearchMatch {
  type: EntityKind;
  id: string;
  title: string;
  score?: number | null;
}

export interface AnswerSearch {
  status: "resolved" | "several" | "ambiguous" | "none" | (string & {});
  phrase?: string | null;
  matches: SearchMatch[];
}

export interface AnswerEntitySummary {
  type: EntityKind;
  id: string;
  title?: string | null;
  found: boolean;
  total_reports?: number | null;
}

export interface AnswerResponse {
  status: AnswerStatus | (string & {});
  /** The whole answer as text. The UI renders `sections`, in order. */
  text: string;
  sections: AnswerSection[];
  search?: AnswerSearch | null;
  entities?: AnswerEntitySummary[] | null;
}

/** An "ambiguous" choice becomes the entity of the re-sent question (handover §5). */
export function matchToEntity(match: SearchMatch): AnswerEntity {
  return { entity_type: match.type, entity_id: match.id };
}
