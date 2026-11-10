/**
 * Parsing for SafeText (handover §4): the bot's text may use **bold**, *italic* and "- " list
 * lines, nothing else. Everything is returned as React text nodes or <strong>/<em> elements,
 * so HTML, links, headings and code in report excerpts show as plain text. No raw HTML.
 *
 * Rebuilt on 2026-11-10 from SafeText.tsx and SafeText.test.tsx because the original file
 * never reached git (the root .gitignore's Python rule lib/ hid src/lib/). The original
 * author should compare it with his version.
 */
import type { ReactNode } from "react";

export type Block = { type: "paragraph"; text: string } | { type: "list"; items: string[] };

/** Each line is a paragraph; consecutive "- " lines form one list. Blank lines are dropped. */
export function toBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    const item = /^\s*- (.*)$/.exec(line);
    if (item) {
      const last = blocks.at(-1);
      if (last && last.type === "list") last.items.push(item[1] ?? "");
      else blocks.push({ type: "list", items: [item[1] ?? ""] });
    } else {
      blocks.push({ type: "paragraph", text: line });
    }
  }
  return blocks;
}

// **bold** or *italic*: no space just inside the stars, no star inside, so "5 * 3" and an
// unclosed "**half" stay as they are.
const INLINE = /\*\*(?=\S)([^*]+?)(?<=\S)\*\*|\*(?=\S)([^*]+?)(?<=\S)\*/g;

/** One line as text and <strong>/<em> elements, in the original order. */
export function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    out.push(m[1] !== undefined ? <strong key={key++}>{m[1]}</strong> : <em key={key++}>{m[2]}</em>);
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
