/**
 * Tests for useIsMobile hook.
 *
 * VAL-19: Covers media query detection and resize handling.
 */
import { renderHook, act } from "@testing-library/react";
import { useIsMobile } from "./use-mobile";

describe("useIsMobile", () => {
  let changeHandler: (() => void) | null = null;

  beforeEach(() => {
    changeHandler = null;
    vi.restoreAllMocks();
  });

  function mockMatchMedia(initialWidth: number) {
    let currentWidth = initialWidth;

    Object.defineProperty(window, "innerWidth", {
      writable: true,
      configurable: true,
      value: currentWidth,
    });

    Object.defineProperty(window, "matchMedia", {
      writable: true,
      configurable: true,
      value: (query: string) => ({
        matches: currentWidth < 768,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: (_: string, handler: () => void) => {
          changeHandler = handler;
        },
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    });

    return {
      resize(width: number) {
        currentWidth = width;
        Object.defineProperty(window, "innerWidth", {
          writable: true,
          configurable: true,
          value: width,
        });
        if (changeHandler) changeHandler();
      },
    };
  }

  it("returns true on mobile width (< 768px)", () => {
    mockMatchMedia(500);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it("returns false on desktop width (>= 768px)", () => {
    mockMatchMedia(1024);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it("returns false at exactly 768px (breakpoint boundary)", () => {
    mockMatchMedia(768);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it("returns true at 767px (just below breakpoint)", () => {
    mockMatchMedia(767);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it("updates when window resizes from desktop to mobile", () => {
    const media = mockMatchMedia(1024);
    const { result } = renderHook(() => useIsMobile());

    expect(result.current).toBe(false);

    act(() => {
      media.resize(500);
    });

    expect(result.current).toBe(true);
  });

  it("updates when window resizes from mobile to desktop", () => {
    const media = mockMatchMedia(400);
    const { result } = renderHook(() => useIsMobile());

    expect(result.current).toBe(true);

    act(() => {
      media.resize(1200);
    });

    expect(result.current).toBe(false);
  });

  it("cleans up event listener on unmount", () => {
    const removeSpy = vi.fn();

    Object.defineProperty(window, "innerWidth", {
      writable: true,
      configurable: true,
      value: 1024,
    });

    Object.defineProperty(window, "matchMedia", {
      writable: true,
      configurable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: removeSpy,
        dispatchEvent: () => false,
      }),
    });

    const { unmount } = renderHook(() => useIsMobile());
    unmount();

    expect(removeSpy).toHaveBeenCalledWith("change", expect.any(Function));
  });
});
