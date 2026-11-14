/**
 * Renders the bot's text safely (handover §4): **bold**, *italic* and "- " lists only.
 * "<script>", HTML tags, links, headings or code in report excerpts show as plain text.
 * Parsing is in lib/safeText.tsx. Never use dangerouslySetInnerHTML for response text.
 */
import { renderInline, toBlocks } from "../lib/safeText";

export function SafeText({ text, className }: { text: string; className?: string }) {
  return (
    <div className={className ? `safe-text ${className}` : "safe-text"}>
      {toBlocks(text).map((block, i) =>
        block.type === "list" ? (
          <ul key={i}>
            {block.items.map((item, j) => (
              <li key={j}>{renderInline(item)}</li>
            ))}
          </ul>
        ) : (
          <p key={i}>{renderInline(block.text)}</p>
        ),
      )}
    </div>
  );
}
