import { useChat } from "../../chat/chatStore";
import { CHAT_TOGGLE_ID } from "./ChatPanel";

/** Opens and closes the assistant panel; in the header on every page. */
export function ChatToggle() {
  const { open, setOpen, pending } = useChat();
  return (
    <button
      id={CHAT_TOGGLE_ID}
      type="button"
      className="btn chat-toggle"
      aria-expanded={open}
      aria-controls="chat-panel"
      onClick={() => setOpen(!open)}
    >
      {open ? "Close assistant" : pending ? "Assistant (working…)" : "Ask the assistant"}
    </button>
  );
}
