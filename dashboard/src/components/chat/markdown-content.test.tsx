/**
 * Tests for MarkdownContent component.
 *
 * Validates: markdown rendering pipeline (bold, lists, code, tables, links, images),
 * inline code styling, CodeBlock delegation, empty/plain text handling.
 *
 * react-markdown v10 renders asynchronously — all content assertions use waitFor.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MarkdownContent } from "./markdown-content";

// ── Helpers ──────────────────────────────────────────────────────────────

function renderMarkdown(content: string) {
  return render(<MarkdownContent content={content} />);
}

/** Wait for markdown to finish rendering, then return the container. */
async function rendered(content: string) {
  const { container } = renderMarkdown(content);
  // Wait for react-markdown async rendering to populate the DOM
  await waitFor(() => {
    const el = container.querySelector("[data-testid='markdown-content']");
    expect(el?.childElementCount).toBeGreaterThan(0);
  });
  return container;
}

// ── Basic rendering ──────────────────────────────────────────────────────

describe("MarkdownContent", () => {
  it("renders bold text as <strong>", async () => {
    const container = await rendered("This is **bold** text");
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe("bold");
  });

  it("renders italic text as <em>", async () => {
    const container = await rendered("This is *italic* text");
    const em = container.querySelector("em");
    expect(em).not.toBeNull();
    expect(em?.textContent).toBe("italic");
  });

  it("renders numbered list as <ol>", async () => {
    const container = await rendered("1. First\n2. Second\n3. Third");
    const ol = container.querySelector("ol");
    expect(ol).not.toBeNull();
    expect(ol?.querySelectorAll("li").length).toBe(3);
  });

  it("renders bullet list as <ul>", async () => {
    const container = await rendered("- Alpha\n- Beta\n- Gamma");
    const ul = container.querySelector("ul");
    expect(ul).not.toBeNull();
    expect(ul?.querySelectorAll("li").length).toBe(3);
  });

  it("renders inline code with styled pill", async () => {
    const container = await rendered("Use `console.log` for debugging");
    const inlineCode = container.querySelector("code");
    expect(inlineCode).not.toBeNull();
    expect(inlineCode?.textContent).toBe("console.log");
    // Inline code should NOT be inside a CodeBlock wrapper
    expect(container.querySelector("[data-testid='copy-code-btn']")).toBeNull();
  });

  it("renders fenced code block inside CodeBlock wrapper", async () => {
    renderMarkdown("```typescript\nconst x = 1;\n```");
    await waitFor(() => {
      expect(screen.getByTestId("copy-code-btn")).toBeDefined();
    });
    expect(screen.getByTestId("code-lang").textContent).toBe("typescript");
    expect(screen.getByTestId("code-content").textContent).toContain("const x = 1;");
  });

  it("renders code block without language with fallback label", async () => {
    renderMarkdown("```\nhello world\n```");
    await waitFor(() => {
      expect(screen.getByTestId("code-lang")).toBeDefined();
    });
    expect(screen.getByTestId("code-lang").textContent).toBe("code");
  });

  it("renders link with target=_blank and rel=noopener noreferrer", async () => {
    const container = await rendered("[click here](https://example.com)");
    const link = container.querySelector("a");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBe("https://example.com");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link?.textContent).toBe("click here");
  });

  it("renders GFM table", async () => {
    const container = await rendered(
      "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |",
    );
    const table = container.querySelector("table");
    expect(table).not.toBeNull();
    // 1 header + 2 data rows
    expect(table?.querySelectorAll("tr").length).toBe(3);
  });

  it("renders blockquote", async () => {
    const container = await rendered("> This is a quote");
    const bq = container.querySelector("blockquote");
    expect(bq).not.toBeNull();
    expect(bq?.textContent).toContain("This is a quote");
  });

  it("renders strikethrough (GFM)", async () => {
    const container = await rendered("This is ~~deleted~~ text");
    const del = container.querySelector("del");
    expect(del).not.toBeNull();
    expect(del?.textContent).toBe("deleted");
  });

  it("renders horizontal rule", async () => {
    const container = await rendered("Above\n\n---\n\nBelow");
    expect(container.querySelector("hr")).not.toBeNull();
  });

  it("renders image with lazy loading and no-referrer", async () => {
    const container = await rendered("![alt text](https://example.com/img.png)");
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("alt")).toBe("alt text");
    expect(img?.getAttribute("src")).toBe("https://example.com/img.png");
    expect(img?.getAttribute("loading")).toBe("lazy");
    expect(img?.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  // ── Edge cases ───────────────────────────────────────────────────────

  it("renders plain text without markdown cleanly", async () => {
    renderMarkdown("Just a normal sentence without any formatting.");
    await waitFor(() => {
      expect(screen.getByTestId("markdown-content").textContent).toContain(
        "Just a normal sentence",
      );
    });
  });

  it("returns null for empty string", () => {
    const { container } = renderMarkdown("");
    expect(container.innerHTML).toBe("");
  });

  it("renders heading levels", async () => {
    // Use separate paragraphs to ensure proper parsing
    const container = await rendered("# Heading One\n\nContent below");
    expect(container.querySelector("h1")?.textContent).toBe("Heading One");
  });

  it("has prose-chat class for chat-specific styling", async () => {
    renderMarkdown("test content");
    await waitFor(() => {
      const el = screen.getByTestId("markdown-content");
      expect(el.textContent).toContain("test content");
    });
    const el = screen.getByTestId("markdown-content");
    expect(el.classList.contains("prose-chat")).toBe(true);
    expect(el.classList.contains("prose-invert")).toBe(true);
    expect(el.classList.contains("max-w-none")).toBe(true);
  });

  it("accepts additional className", () => {
    render(<MarkdownContent content="test" className="my-custom-class" />);
    const container = screen.getByTestId("markdown-content");
    expect(container.classList.contains("my-custom-class")).toBe(true);
  });

  it("renders multiple paragraphs", async () => {
    const container = await rendered("First paragraph.\n\nSecond paragraph.");
    const paragraphs = container.querySelectorAll(
      "[data-testid='markdown-content'] > p",
    );
    expect(paragraphs.length).toBe(2);
  });
});
