import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SafeText } from "./SafeText";

describe("SafeText: safe rendering of the bot's text (handover §4, §8)", () => {
  it("shows <script> and HTML in an excerpt as text, never as elements", () => {
    const text =
      "- *Injury, received 2025-09-28* (report 1): \"<script>alert('x')</script> <b>not bold</b> <img src=x onerror=alert(1)>\"";
    const { container } = render(<SafeText text={text} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<script>alert('x')</script>");
    expect(container.textContent).toContain("<b>not bold</b>");
  });

  it("does not turn other Markdown into links, headings or code", () => {
    const { container } = render(<SafeText text={"# not a heading [a link](https://example.org) `not code`"} />);
    expect(container.querySelector("a, h1, code")).toBeNull();
    expect(container.textContent).toBe("# not a heading [a link](https://example.org) `not code`");
  });

  it("renders **bold**, *italic* and '- ' lists", () => {
    const { container } = render(
      <SafeText text={"**Recalls:** 2 found. Newest first:\n- Class II, *ongoing*\n- Class III"} />,
    );
    expect(screen.getByText("Recalls:").tagName).toBe("STRONG");
    expect(screen.getByText("ongoing").tagName).toBe("EM");
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });

  it("keeps every word in the original order", () => {
    const text = "**A** first line\n- *b* item one\n- item two\nlast line with a lone * star";
    const { container } = render(<SafeText text={text} />);
    const shown = Array.from(container.querySelectorAll("p, li"), (el) => el.textContent);
    expect(shown).toEqual(["A first line", "b item one", "item two", "last line with a lone * star"]);
  });

  it("leaves unmatched asterisks as they are", () => {
    const { container } = render(<SafeText text={"5 * 3 and a **half"} />);
    expect(container.querySelector("strong, em")).toBeNull();
    expect(container.textContent).toBe("5 * 3 and a **half");
  });
});
