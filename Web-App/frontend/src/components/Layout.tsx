import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useChat } from "../chat/chatStore";
import { config } from "../config";
import { paths } from "../routes";
import { ChatPanel } from "./chat/ChatPanel";
import { ChatToggle } from "./chat/ChatToggle";

export function Layout({ children }: { children: ReactNode }) {
  const { open } = useChat();
  return (
    <div className={open ? "app chat-open" : "app"}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="site-header">
        <Link to={paths.search()} className="brand">
          Vigil MD
        </Link>
        {config.useMocks && (
          <span className="mock-badge" title="Answers come from example files, not the API">
            Example data
          </span>
        )}
        <span className="header-spacer" />
        <ChatToggle />
      </header>
      <div className="shell">
        <main id="main" className="site-main">
          {children}
        </main>
        {open && <ChatPanel />}
      </div>
      <footer className="site-footer">
        Vigil MD shows what FDA adverse-event reports and recalls say. It does not give medical advice.
      </footer>
    </div>
  );
}
