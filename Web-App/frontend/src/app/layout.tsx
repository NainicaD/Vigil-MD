import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { ChatProvider } from "../chat/ChatProvider";
import { Layout } from "../components/Layout";
import "../styles.css";

export const metadata: Metadata = {
  title: "Vigil MD",
  description: "Search FDA adverse-event reports and recalls for medical devices.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

/** Every page: header, the page, the assistant panel and the footer (handover §3). */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ChatProvider>
          <Layout>{children}</Layout>
        </ChatProvider>
      </body>
    </html>
  );
}
