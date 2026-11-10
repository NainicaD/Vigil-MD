/**
 * The assistant conversation: one per browser tab, kept in memory only. It survives moving
 * between pages and is gone on reload. Nothing is written to localStorage, cookies or the
 * console (handover §7).
 */
import { createContext, useCallback, useContext, useRef, useState } from "react";
import { api } from "../api/client";
import { SERVICE_UNAVAILABLE_MESSAGE } from "../api/errors";
import { matchToEntity, type AnswerEntity, type AnswerResponse, type SearchMatch } from "../api/types";

export interface TurnBase {
  id: number;
  question: string;
  /** Sent with the question: the device page's entity, or an "ambiguous" choice. */
  entity: AnswerEntity | null;
  /** Title of that entity, shown with the question. */
  about: string | null;
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

  const send = useCallback((question: string, entity: AnswerEntity | null, about: string | null = null) => {
    const q = question.trim();
    if (!q || busy.current) return;
    busy.current = true;
    const id = nextId.current++;
    setTurns((t) => [...t, { id, question: q, entity, about, state: "pending" }]);
    const settle = (update: Turn) => setTurns((t) => t.map((x) => (x.id === id ? update : x)));
    api
      .answer(q, entity)
      .then((response) => settle({ id, question: q, entity, about, state: "done", response }))
      // Network failure or no answer within 30 s: the handover's own short message.
      .catch(() => settle({ id, question: q, entity, about, state: "failed", message: SERVICE_UNAVAILABLE_MESSAGE }))
      .finally(() => {
        busy.current = false;
      });
  }, []);

  const retry = useCallback((turn: Turn) => send(turn.question, turn.entity, turn.about), [send]);

  const choose = useCallback(
    (turn: Turn, match: SearchMatch) => send(turn.question, matchToEntity(match), match.title),
    [send],
  );

  const clear = useCallback(() => {
    if (!busy.current) setTurns([]);
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
