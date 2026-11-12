"use client";

import Link from "next/link";
import { useEffect } from "react";
import { AnswerInspector } from "../components/dev/AnswerInspector";
import { ServiceStatus } from "../components/dev/ServiceStatus";
import { SessionLog } from "../components/dev/SessionLog";
import { paths } from "../routes";

/**
 * The developer page (/developer): only in builds with NEXT_PUBLIC_DEV_TOOLS=true. Technical
 * details for the team; the client pages (search, device page, assistant) never show them.
 */
export function DeveloperPage() {
  useEffect(() => {
    document.title = "Developer · Vigil MD";
    return () => {
      document.title = "Vigil MD";
    };
  }, []);

  return (
    <div className="page dev-page">
      <p className="back">
        <Link href={paths.search()}>← Client site</Link>
      </p>
      <h1>Developer</h1>
      <p className="lead">
        How the service is running and how each answer is made. For the team only: this page exists only in builds with{" "}
        <code>NEXT_PUBLIC_DEV_TOOLS=true</code>, and nothing on it is stored.
      </p>
      <ServiceStatus />
      <AnswerInspector />
      <SessionLog />
    </div>
  );
}
