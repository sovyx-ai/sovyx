/**
 * Tests for CodeBlock component.
 *
 * Validates: rendering, language label, copy button mechanics (clipboard + feedback),
 * i18n integration, fallback labels, textContent extraction from ReactNode children.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import "@/lib/i18n";

import { CodeBlock } from "./code-block";

// ── Clipboard mock ───────────────────────────────────────────────────────

const writeTextMock = vi.fn().mockResolvedValue(undefined);

// Polyfill clipboard for jsdom (read-only by default)
Object.defineProperty(navigator, "clipboard", {
  value: { writeText: writeTextMock },
  writable: true,
  configurable: true,
});

// ── Rendering ────────────────────────────────────────────────────────────

describe("CodeBlock", () => {
  it("renders children content", () => {
    render(
      <CodeBlock language="python">
        <pre><code>print(&quot;hello&quot;)</code></pre>
      </CodeBlock>,
    );
    expect(screen.getByTestId("code-content").textContent).toContain('print("hello")');
  });

  it("shows language label when provided", () => {
    render(<CodeBlock language="typescript"><pre><code>const x = 1;</code></pre></CodeBlock>);
    expect(screen.getByTestId("code-lang").textContent).toBe("typescript");
  });

  it("shows fallback 'code' label when no language", () => {
    render(<CodeBlock><pre><code>hello</code></pre></CodeBlock>);
    expect(screen.getByTestId("code-lang").textContent).toBe("code");
  });

  it("renders ReactNode children (highlighted spans)", () => {
    render(
      <CodeBlock language="js">
        <pre>
          <code>
            <span className="hljs-keyword">const</span>{" "}x = <span className="hljs-number">1</span>
          </code>
        </pre>
      </CodeBlock>,
    );
    const content = screen.getByTestId("code-content");
    expect(content.textContent).toContain("const");
    expect(content.querySelector(".hljs-keyword")).not.toBeNull();
  });

  // ── Copy button (real timers — async clipboard needs microtask resolution) ──

  it("copy button extracts text via textContent and writes to clipboard", async () => {
    writeTextMock.mockClear();
    render(
      <CodeBlock language="bash">
        <pre><code>echo hello</code></pre>
      </CodeBlock>,
    );

    fireEvent.click(screen.getByTestId("copy-code-btn"));

    await waitFor(() => {
      expect(writeTextMock).toHaveBeenCalledOnce();
    });
    expect(writeTextMock).toHaveBeenCalledWith("echo hello");
  });

  it("shows 'Copied' feedback after click", async () => {
    writeTextMock.mockClear();
    render(<CodeBlock><pre><code>test</code></pre></CodeBlock>);

    const btn = screen.getByTestId("copy-code-btn");
    expect(btn.textContent).toContain("Copy");

    fireEvent.click(btn);

    await waitFor(() => {
      expect(btn.textContent).toContain("Copied");
    });
  });

  it("reverts to 'Copy' after feedback timeout", async () => {
    writeTextMock.mockClear();
    vi.useFakeTimers();

    render(<CodeBlock><pre><code>test</code></pre></CodeBlock>);

    // Trigger the click handler — flush the microtask queue for async clipboard
    await act(async () => {
      fireEvent.click(screen.getByTestId("copy-code-btn"));
      // Let the async clipboard.writeText promise resolve
      await Promise.resolve();
    });

    const btn = screen.getByTestId("copy-code-btn");
    expect(btn.textContent).toContain("Copied");

    // Advance past the COPIED_FEEDBACK_MS (1500ms)
    act(() => {
      vi.advanceTimersByTime(1500);
    });

    expect(btn.textContent).toContain("Copy");

    vi.useRealTimers();
  });

  // ── Accessibility ────────────────────────────────────────────────────

  it("copy button has aria-label for screen readers", () => {
    render(<CodeBlock><pre><code>x</code></pre></CodeBlock>);
    expect(screen.getByTestId("copy-code-btn").getAttribute("aria-label")).toBe("Copy code");
  });

  it("copy button is type=button (no form submission)", () => {
    render(<CodeBlock><pre><code>x</code></pre></CodeBlock>);
    expect(screen.getByTestId("copy-code-btn").getAttribute("type")).toBe("button");
  });
});
