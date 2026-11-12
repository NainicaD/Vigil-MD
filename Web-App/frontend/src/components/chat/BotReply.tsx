import Link from "next/link";
import type { AnswerSection, SearchMatch } from "../../api/types";
import type { Turn } from "../../chat/chatStore";
import { sectionsToShow } from "../../lib/answer";
import { paths } from "../../routes";
import { SafeText } from "../SafeText";

export const LOOKING_UP_MESSAGE = "Looking this up…";

interface Props {
  turn: Turn;
  /** Another answer is awaited: buttons that send are disabled. */
  busy: boolean;
  onChoose: (match: SearchMatch) => void;
  onRetry: () => void;
  /** Called when "Search the device list" is used (e.g. to close the panel on a phone). */
  onNavigate?: () => void;
}

/**
 * One reply, shown according to its status (handover §5). The bot's sections are shown as
 * given and in the order received; this component only adds the buttons and links the
 * status table asks for.
 */
export function BotReply({ turn, busy, onChoose, onRetry, onNavigate }: Props) {
  if (turn.state === "pending") {
    return (
      <div className="reply reply-pending" role="status">
        {LOOKING_UP_MESSAGE}
      </div>
    );
  }

  if (turn.state === "failed") {
    return (
      <div className="reply" role="alert">
        <p>{turn.message}</p>
        <RetryButton busy={busy} onRetry={onRetry} />
      </div>
    );
  }

  const r = turn.response;
  return (
    <div className="reply" data-status={r.status}>
      {sectionsToShow(r).map((s, i) => (
        <Section key={i} section={s} />
      ))}

      {r.status === "ambiguous" && (
        <ChoiceButtons matches={r.search?.matches ?? []} busy={busy} onChoose={onChoose} />
      )}
      {r.status === "not_found" && (
        <p className="reply-extra">
          <Link href={paths.search()} onClick={onNavigate}>
            Search the device list
          </Link>
        </p>
      )}
      {r.status === "error" && <RetryButton busy={busy} onRetry={onRetry} />}
    </div>
  );
}

function Section({ section }: { section: AnswerSection }) {
  if (section.kind === "medical_refusal") {
    // Fixed text, shown first and unchanged; a calm note, not an error (handover §4).
    return <SafeText className="section refusal-note" text={section.text} />;
  }
  return <SafeText className={`section section-${section.kind}`} text={section.text} />;
}

function ChoiceButtons({
  matches,
  busy,
  onChoose,
}: {
  matches: SearchMatch[];
  busy: boolean;
  onChoose: (match: SearchMatch) => void;
}) {
  if (matches.length === 0) return null;
  return (
    <div className="choices" role="group" aria-label="Choose the one you mean">
      {matches.map((m) => (
        <button key={`${m.type}|${m.id}`} type="button" className="choice" disabled={busy} onClick={() => onChoose(m)}>
          {m.title}
        </button>
      ))}
    </div>
  );
}

function RetryButton({ busy, onRetry }: { busy: boolean; onRetry: () => void }) {
  return (
    <button type="button" className="btn secondary" disabled={busy} onClick={onRetry}>
      Try again
    </button>
  );
}
