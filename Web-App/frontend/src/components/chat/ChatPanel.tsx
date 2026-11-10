import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { MAX_QUESTION_LENGTH } from "../../api/types";
import { useChat } from "../../chat/chatStore";
import { BotReply } from "./BotReply";

export const CHAT_TOGGLE_ID = "chat-toggle";

/**
 * The assistant panel, available on every page (handover §3). On a device page the
 * question is sent with that device as `entity`; elsewhere `entity: null`.
 * Side panel on wide screens, full screen on phones. Esc closes it.
 */
export function ChatPanel() {
  const chat = useChat();
  const { turns, pending, pageEntity, setOpen } = chat;
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // Focus the input when the panel opens; give focus back to the toggle when it closes.
  useEffect(() => {
    inputRef.current?.focus();
    return () => document.getElementById(CHAT_TOGGLE_ID)?.focus();
  }, []);

  // Keep the newest reply in view.
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [turns]);

  // "need_device": put the focus back in the input (handover §5).
  const last = turns[turns.length - 1];
  const lastNeedsDevice = last?.state === "done" && last.response.status === "need_device";
  useEffect(() => {
    if (lastNeedsDevice) inputRef.current?.focus();
  }, [lastNeedsDevice, last?.id]);

  function submit(e?: FormEvent) {
    e?.preventDefault();
    if (pending || !text.trim()) return;
    chat.send(text, pageEntity?.entity ?? null, pageEntity?.title ?? null);
    setText("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLElement>) {
    if (e.key === "Escape") {
      e.stopPropagation();
      setOpen(false);
    }
  }

  function onInputKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter adds a new line.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  const closeOnPhone = () => {
    if (window.matchMedia?.("(max-width: 720px)").matches) setOpen(false);
  };

  return (
    <aside id="chat-panel" className="chat-panel" aria-labelledby="chat-title" onKeyDown={onKeyDown}>
      <div className="chat-head">
        <h2 id="chat-title">Assistant</h2>
        <div className="chat-head-actions">
          {turns.length > 0 && (
            <button type="button" className="btn secondary small" onClick={chat.clear} disabled={pending}>
              Clear
            </button>
          )}
          <button type="button" className="icon-btn" onClick={() => setOpen(false)} aria-label="Close the assistant">
            ×
          </button>
        </div>
      </div>

      <p className="chat-context">
        {pageEntity ? (
          <>
            Questions here are about <strong>{pageEntity.title}</strong>.
          </>
        ) : (
          "Ask about a device, a company or an FDA product code."
        )}
      </p>

      <div className="chat-log" aria-live="polite" aria-relevant="additions">
        {turns.length === 0 && (
          <p className="chat-empty">
            The assistant reports what FDA adverse-event reports and recalls say. It does not give medical advice.
          </p>
        )}
        {turns.map((turn) => (
          <div key={turn.id} className="turn">
            <div className="question">
              {turn.about && <span className="question-about">About: {turn.about}</span>}
              <span className="question-text">{turn.question}</span>
            </div>
            <BotReply
              turn={turn}
              busy={pending}
              onChoose={(m) => chat.choose(turn, m)}
              onRetry={() => chat.retry(turn)}
              onNavigate={closeOnPhone}
            />
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <form className="chat-form" onSubmit={submit}>
        <label htmlFor="chat-input" className="visually-hidden">
          Your question
        </label>
        <textarea
          id="chat-input"
          ref={inputRef}
          className="chat-input"
          rows={2}
          value={text}
          maxLength={MAX_QUESTION_LENGTH}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onInputKeyDown}
          placeholder={pageEntity ? `Ask about ${pageEntity.title}` : "Ask a question"}
        />
        <button type="submit" className="btn" disabled={pending || !text.trim()}>
          {pending ? "Wait…" : "Ask"}
        </button>
      </form>
    </aside>
  );
}
