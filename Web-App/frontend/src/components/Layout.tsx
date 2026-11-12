"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useChat } from "../chat/chatStore";
import { config } from "../config";
import { paths } from "../routes";
import { ChatPanel } from "./chat/ChatPanel";
import { ChatToggle } from "./chat/ChatToggle";

export function Layout({ children }: { children: ReactNode }) {
  const { open } = useChat();
  const onDeveloperPage = usePathname() === paths.developer();
  return (
    <div className={open ? "app chat-open" : "app"}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="site-header">
        <Link href={paths.search()} className="brand">
          Vigil MD
        </Link>
        {config.useMocks && (
          <span className="mock-badge" title="Answers come from example files, not the API">
            Example data
          </span>
        )}
        <span className="header-spacer" />
        {config.devTools && (
          <Link className="dev-link" href={onDeveloperPage ? paths.search() : paths.developer()}>
            {onDeveloperPage ? "Client site" : "Developer"}
          </Link>
        )}
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
