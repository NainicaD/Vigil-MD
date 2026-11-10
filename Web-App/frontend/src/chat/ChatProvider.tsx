import type { ReactNode } from "react";
import { ChatContext, useChatController } from "./chatStore";

export function ChatProvider({ children }: { children: ReactNode }) {
  const controller = useChatController();
  return <ChatContext.Provider value={controller}>{children}</ChatContext.Provider>;
}
