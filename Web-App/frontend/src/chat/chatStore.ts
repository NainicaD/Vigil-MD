/**
 * The assistant conversation: one per browser tab, kept in memory only. It survives moving
 * between pages and is gone on reload. Nothing is written to localStorage, cookies or the
 * console (handover §7).
 *
 * Short-term memory: the last finished turn is kept (one turn back, in memory) and sent as
 * `context` with a question that has no page entity, so "any deaths?" then "DreamStation", or
 * "what about recalls?" after an answer, work. The server keeps nothing between questions.
 */
import { createContext, useCallback, useContext, useRef, useState } from "react";
import { api } from "../api/client";
import { failureMessage } from "../api/errors";
import {
  MAX_QUESTION_LENGTH,
  matchToEntity,
  type AnswerContext,
  type AnswerEntity,
  type AnswerResponse,
  type SearchMatch,
} from "../api/types";

export interface TurnBase {
  id: number;
  question: string;
  /** Sent with the question: the device page's entity, or an "ambiguous" choice. */
  entity: AnswerEntity | null;
  /** Title of that entity, shown with the question. */
  about: string | null;
  /** The previous turn sent with the question (null with a page entity or no earlier answer). */
  context: AnswerContext | null;
}

/** A finished question and its answer: what the next question's context is built from. */
export interface FinishedTurn {
  question: string;
  response: AnswerResponse;
}

/** The context for the next question: the last answer's question, status and its entity
 * when it had exactly one found entity. */
export function contextFrom(last: FinishedTurn | null): AnswerContext | null {
  if (!last) return null;
  const found = (last.response.entities ?? []).filter((e) => e.found);
  const only = found.length === 1 ? found[0] : undefined;
  return {
    previous_question: last.question.slice(0, MAX_QUESTION_LENGTH),
    previous_status: last.response.status,
    entity: only ? { entity_type: only.type, entity_id: only.id } : null,
  };
}

/** "DreamStation (from your previous question)" when an answer used the previous turn. */
export function contextAbout(turn: Turn): string | null {
  if (turn.state !== "done" || !turn.response.context_used) return null;
  const found = (turn.response.entities ?? []).filter((e) => e.found);
  const names = found.map((e) => e.title || e.id);
  return names.length ? `${names.join(", ")} (from your previous question)` : null;
}

export type Turn =
  | (TurnBase & { state: "pending" })
  | (TurnBase & { state: "done"; response: AnswerResponse })
  | (TurnBase & { state: "failed"; message: string });

/** The entity of the page being viewed, if any (set by the device page). */
export interface PageEntity {
  entity: AnswerEntity;
  title: string;
}

export interface ChatController {
  turns: Turn[];
  /** True while an answer is awaited: a second send is blocked (handover §5). */
  pending: boolean;
  send: (question: string, entity: AnswerEntity | null, about?: string | null) => void;
  /** Ask the same question again (after "error" or a network failure). */
  retry: (turn: Turn) => void;
  /** Re-ask an "ambiguous" question about the chosen entry. */
  choose: (turn: Turn, match: SearchMatch) => void;
  clear: () => void;
  open: boolean;
  setOpen: (open: boolean) => void;
  pageEntity: PageEntity | null;
  setPageEntity: (entity: PageEntity | null) => void;
}

export const ChatContext = createContext<ChatController | null>(null);

export function useChat(): ChatController {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChat must be used inside <ChatProvider>");
  return ctx;
}

export function useChatController(): ChatController {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [open, setOpen] = useState(false);
  const [pageEntity, setPageEntity] = useState<PageEntity | null>(null);
  const nextId = useRef(1);
  const busy = useRef(false);
  /** The last finished turn (one turn back), for the next question's context. */
  const lastFinished = useRef<FinishedTurn | null>(null);

  const ask = useCallback(
    (question: string, entity: AnswerEntity | null, about: string | null, context: AnswerContext | null) => {
      const q = question.trim();
      if (!q || busy.current) return;
      busy.current = true;
      const id = nextId.current++;
      const base = { id, question: q, entity, about, context };
      setTurns((t) => [...t, { ...base, state: "pending" }]);
      const settle = (update: Turn) => setTurns((t) => t.map((x) => (x.id === id ? update : x)));
      api
        .answer(q, entity, context ? { context } : {})
        .then((response) => {
          lastFinished.current = { question: q, response };
          settle({ ...base, state: "done", response });
        })
        // The API's own error text (rate limit, budget…), else the handover's short message.
        .catch((error: unknown) => settle({ ...base, state: "failed", message: failureMessage(error) }))
        .finally(() => {
          busy.current = false;
        });
    },
    [],
  );

  // A page entity (or an "ambiguous" choice) is explicit: no context is sent with it.
  const send = useCallback(
    (question: string, entity: AnswerEntity | null, about: string | null = null) =>
      ask(question, entity, about, entity ? null : contextFrom(lastFinished.current)),
    [ask],
  );

  // The same question with the same context it was first sent with.
  const retry = useCallback((turn: Turn) => ask(turn.question, turn.entity, turn.about, turn.context), [ask]);

  const choose = useCallback(
    (turn: Turn, match: SearchMatch) => send(turn.question, matchToEntity(match), match.title),
    [send],
  );

  const clear = useCallback(() => {
    if (busy.current) return;
    setTurns([]);
    lastFinished.current = null;            // the memory goes with the conversation
  }, []);

  return {
    turns,
    pending: turns.some((t) => t.state === "pending"),
    send,
    retry,
    choose,
    clear,
    open,
    setOpen,
    pageEntity,
    setPageEntity,
  };
}
