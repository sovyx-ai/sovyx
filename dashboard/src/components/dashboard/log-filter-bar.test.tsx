/**
 * LogFilterBar component tests — Phase 12.11.
 *
 * Pins debounced free-text fields, immediate single-action selectors
 * (level, datetime, saga_id), level toggle behaviour, autocomplete
 * deduplication, and the reset button.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@/test/test-utils";

import {
  LogFilterBar,
  type LogFilterState,
} from "./log-filter-bar";

function mkFilters(overrides: Partial<LogFilterState> = {}): LogFilterState {
  return {
    q: "",
    level: null,
    logger: "",
    saga_id: "",
    since: "",
    until: "",
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
});

describe("LogFilterBar", () => {
  it("renders all filter labels and the reset button", () => {
    render(
      <LogFilterBar filters={mkFilters()} onChange={vi.fn()} onReset={vi.fn()} />,
    );
    expect(screen.getByText("Search")).toBeInTheDocument();
    expect(screen.getByText("Level")).toBeInTheDocument();
    expect(screen.getByText("Logger")).toBeInTheDocument();
    expect(screen.getByText("Saga ID")).toBeInTheDocument();
    expect(screen.getByText("Since")).toBeInTheDocument();
    expect(screen.getByText("Until")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Reset filters/i })).toBeInTheDocument();
  });

  it("renders all five level pills plus the 'All' pill", () => {
    render(
      <LogFilterBar filters={mkFilters()} onChange={vi.fn()} onReset={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "All" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "DEBUG" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "INFO" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "WARNING" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "ERROR" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "CRITICAL" })).toBeInTheDocument();
  });

  it("debounces search-text changes by 300 ms before calling onChange", () => {
    const onChange = vi.fn();
    render(
      <LogFilterBar filters={mkFilters()} onChange={onChange} onReset={vi.fn()} />,
    );
    const search = screen.getByPlaceholderText("FTS5 query…");
    fireEvent.change(search, { target: { value: "boot" } });

    // Pre-debounce: parent has not been notified yet.
    vi.advanceTimersByTime(299);
    expect(onChange).not.toHaveBeenCalled();

    // Cross the 300 ms threshold and the patch fires once.
    vi.advanceTimersByTime(1);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenLastCalledWith({ q: "boot" });
  });

  it("collapses rapid keystrokes into a single debounced onChange", () => {
    const onChange = vi.fn();
    render(
      <LogFilterBar filters={mkFilters()} onChange={onChange} onReset={vi.fn()} />,
    );
    const search = screen.getByPlaceholderText("FTS5 query…");
    fireEvent.change(search, { target: { value: "b" } });
    fireEvent.change(search, { target: { value: "bo" } });
    fireEvent.change(search, { target: { value: "boot" } });
    vi.advanceTimersByTime(300);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenLastCalledWith({ q: "boot" });
  });

  it("debounces logger changes independently from the search box", () => {
    const onChange = vi.fn();
    render(
      <LogFilterBar filters={mkFilters()} onChange={onChange} onReset={vi.fn()} />,
    );
    const loggerInput = screen.getByPlaceholderText("sovyx.brain");
    fireEvent.change(loggerInput, { target: { value: "sovyx.voice" } });
    vi.advanceTimersByTime(300);
    expect(onChange).toHaveBeenCalledWith({ logger: "sovyx.voice" });
  });

  it("propagates level clicks immediately (no debounce)", () => {
    const onChange = vi.fn();
    render(
      <LogFilterBar filters={mkFilters()} onChange={onChange} onReset={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "ERROR" }));
    expect(onChange).toHaveBeenCalledWith({ level: "ERROR" });
  });

  it("clicking the active level pill toggles it back to null", () => {
    const onChange = vi.fn();
    render(
      <LogFilterBar
        filters={mkFilters({ level: "ERROR" })}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "ERROR" }));
    expect(onChange).toHaveBeenCalledWith({ level: null });
  });

  it("propagates saga_id changes immediately", () => {
    const onChange = vi.fn();
    render(
      <LogFilterBar filters={mkFilters()} onChange={onChange} onReset={vi.fn()} />,
    );
    const sagaInput = screen.getByPlaceholderText("saga-uuid");
    fireEvent.change(sagaInput, { target: { value: "abc-123" } });
    expect(onChange).toHaveBeenCalledWith({ saga_id: "abc-123" });
  });

  it("invokes onReset when the reset button is clicked", () => {
    const onReset = vi.fn();
    render(
      <LogFilterBar filters={mkFilters()} onChange={vi.fn()} onReset={onReset} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Reset filters/i }));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("re-syncs local input state when canonical filters change from outside", () => {
    const { rerender } = render(
      <LogFilterBar
        filters={mkFilters({ q: "first" })}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    );
    const search = screen.getByPlaceholderText("FTS5 query…") as HTMLInputElement;
    expect(search.value).toBe("first");
    rerender(
      <LogFilterBar
        filters={mkFilters({ q: "outside-update" })}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    );
    expect(search.value).toBe("outside-update");
  });

  it("renders a deduplicated, sorted datalist of known loggers", () => {
    const { container } = render(
      <LogFilterBar
        filters={mkFilters()}
        onChange={vi.fn()}
        onReset={vi.fn()}
        knownLoggers={["sovyx.voice", "sovyx.brain", "sovyx.voice", "sovyx.engine"]}
      />,
    );
    const options = container.querySelectorAll("datalist > option");
    const values = Array.from(options).map((o) => o.getAttribute("value"));
    expect(values).toEqual(["sovyx.brain", "sovyx.engine", "sovyx.voice"]);
  });

  it("omits the datalist when knownLoggers is empty", () => {
    const { container } = render(
      <LogFilterBar
        filters={mkFilters()}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    );
    expect(container.querySelector("datalist")).toBeNull();
  });

  it("caps the datalist suggestions at 50 entries", () => {
    const many = Array.from({ length: 80 }, (_, i) => `sovyx.module${i}`);
    const { container } = render(
      <LogFilterBar
        filters={mkFilters()}
        onChange={vi.fn()}
        onReset={vi.fn()}
        knownLoggers={many}
      />,
    );
    expect(container.querySelectorAll("datalist > option").length).toBe(50);
  });
});
