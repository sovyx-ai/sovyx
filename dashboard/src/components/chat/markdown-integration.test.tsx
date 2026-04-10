/**
 * Integration tests for the markdown rendering pipeline.
 *
 * End-to-end: markdown string → MarkdownContent → CodeBlock → rendered DOM.
 * Validates the full pipeline including rehype-highlight, remark-gfm, and
 * component overrides working together.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import "@/lib/i18n";

import { MarkdownContent } from "./markdown-content";

// Clipboard mock for copy tests
const writeTextMock = vi.fn().mockResolvedValue(undefined);
Object.defineProperty(navigator, "clipboard", {
  value: { writeText: writeTextMock },
  writable: true,
  configurable: true,
});

// ── Helpers ──────────────────────────────────────────────────────────────

async function rendered(content: string) {
  const { container } = render(<MarkdownContent content={content} />);
  await waitFor(() => {
    const el = container.querySelector("[data-testid='markdown-content']");
    expect(el?.childElementCount).toBeGreaterThan(0);
  });
  return container;
}

// ── Full pipeline ────────────────────────────────────────────────────────

describe("Markdown Integration", () => {
  it("renders a complex message with all element types", async () => {
    const markdown = [
      "# Summary",
      "",
      "Here is **bold** and *italic* and `inline code`.",
      "",
      "1. First item",
      "2. Second item",
      "",
      "- Bullet A",
      "- Bullet B",
      "",
      "> A wise quote",
      "",
      "| Col A | Col B |",
      "|-------|-------|",
      "| val 1 | val 2 |",
      "",
      "[link](https://example.com)",
      "",
      "---",
      "",
      "```python",
      "def hello():",
      '    print("world")',
      "```",
    ].join("\n");

    const container = await rendered(markdown);

    // Heading
    expect(container.querySelector("h1")?.textContent).toBe("Summary");
    // Bold + italic
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.querySelector("em")?.textContent).toBe("italic");
    // Lists
    expect(container.querySelector("ol")?.querySelectorAll("li").length).toBe(2);
    expect(container.querySelector("ul")?.querySelectorAll("li").length).toBe(2);
    // Blockquote
    expect(container.querySelector("blockquote")).not.toBeNull();
    // Table
    expect(container.querySelector("table")).not.toBeNull();
    // Link
    const link = container.querySelector("a");
    expect(link?.getAttribute("target")).toBe("_blank");
    // HR
    expect(container.querySelector("hr")).not.toBeNull();
    // Code block with CodeBlock wrapper
    expect(screen.getByTestId("code-lang").textContent).toBe("python");
    expect(screen.getByTestId("code-content").textContent).toContain("def hello():");
  });

  it("message with only plain text renders cleanly (no stray elements)", async () => {
    const container = await rendered(
      "Hello! How can I help you today? Let me know if you need anything.",
    );

    // Should produce a simple paragraph, no code blocks/tables/etc
    expect(container.querySelector("table")).toBeNull();
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector("blockquote")).toBeNull();
    expect(container.querySelector("h1")).toBeNull();

    const p = container.querySelector("p");
    expect(p?.textContent).toContain("Hello! How can I help you today?");
  });

  it("very long code block has overflow-x-auto container", async () => {
    const longLine = "x".repeat(500);
    await rendered(`\`\`\`\n${longLine}\n\`\`\``);

    const codeContent = screen.getByTestId("code-content");
    expect(codeContent.classList.contains("overflow-x-auto")).toBe(true);
  });

  it("code block copy button copies correct text", async () => {
    writeTextMock.mockClear();
    render(
      <MarkdownContent
        content={"```js\nconst answer = 42;\n```"}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("copy-code-btn")).toBeDefined();
    });

    fireEvent.click(screen.getByTestId("copy-code-btn"));

    await waitFor(() => {
      expect(writeTextMock).toHaveBeenCalledOnce();
    });

    // textContent should be the raw code, not include language label
    const copied = writeTextMock.mock.calls[0][0] as string;
    expect(copied).toContain("const answer = 42;");
  });

  it("XSS attempt is sanitized (react-markdown is safe by default)", async () => {
    const xss = '<script>alert("xss")</script>\n\n**safe text**';
    const container = await rendered(xss);

    // No script tags in DOM
    expect(container.querySelector("script")).toBeNull();
    // Safe content renders
    expect(container.querySelector("strong")?.textContent).toBe("safe text");
  });

  it("multiple code blocks in one message each get their own CodeBlock", async () => {
    const md = [
      "```python",
      "print('a')",
      "```",
      "",
      "Some text between.",
      "",
      "```bash",
      "echo hello",
      "```",
    ].join("\n");

    await rendered(md);

    const copyButtons = screen.getAllByTestId("copy-code-btn");
    expect(copyButtons.length).toBe(2);

    const langLabels = screen.getAllByTestId("code-lang");
    expect(langLabels[0].textContent).toBe("python");
    expect(langLabels[1].textContent).toBe("bash");
  });

  it("nested formatting renders correctly", async () => {
    const container = await rendered("**bold with `code` inside** and *italic*");
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    // Inline code inside bold
    const code = strong?.querySelector("code");
    expect(code?.textContent).toBe("code");
  });

  it("GFM autolink renders URLs as clickable links", async () => {
    const container = await rendered("Visit https://example.com for more info.");
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.com");
    expect(link?.getAttribute("target")).toBe("_blank");
  });
});
