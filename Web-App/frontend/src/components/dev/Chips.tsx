/** A short list of codes (labels, rules, query names) as chips; "none" when empty. */
export function Chips({ items, empty = "none" }: { items: string[] | null | undefined; empty?: string }) {
  if (items === null || items === undefined) return <span className="muted">not in this response</span>;
  if (items.length === 0) return <span className="muted">{empty}</span>;
  return (
    <ul className="chips">
      {items.map((item) => (
        <li key={item} className="chip">
          {item}
        </li>
      ))}
    </ul>
  );
}
