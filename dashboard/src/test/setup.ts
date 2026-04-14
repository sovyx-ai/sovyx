import "@testing-library/jest-dom/vitest";

// Mock localStorage
const storage = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  value: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
    clear: () => storage.clear(),
    get length() { return storage.size; },
    key: (i: number) => [...storage.keys()][i] ?? null,
  },
});

// Mock ResizeObserver. TanStack Virtual (and other libs) read the scroll
// element's size through the observer callback — if observe() is a no-op
// the virtualizer stays at 0 items forever in jsdom. Fire the callback
// synchronously with a reasonable rect on observe so virtualized lists
// actually render content for assertion.
class MockResizeObserver {
  constructor(private readonly cb: ResizeObserverCallback) {}
  observe(target: Element): void {
    this.cb(
      [
        {
          target,
          contentRect: {
            x: 0,
            y: 0,
            width: 800,
            height: 600,
            top: 0,
            left: 0,
            right: 800,
            bottom: 600,
            toJSON() {
              return this;
            },
          } as DOMRectReadOnly,
          borderBoxSize: [],
          contentBoxSize: [],
          devicePixelContentBoxSize: [],
        },
      ],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}
window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

// Mock Element.getAnimations (used by @base-ui ScrollArea)
Element.prototype.getAnimations = () => [];

// jsdom reports 0 for `offsetWidth`/`offsetHeight` — TanStack Virtual
// derives its viewport rect from those, so with jsdom defaults every
// virtualizer ends up with zero items and renders nothing. Patch both
// to return a reasonable viewport so virtualized lists surface their
// rows for content-level assertions. Individual tests needing exact
// geometry can still override on the element of interest.
Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
  configurable: true,
  get: () => 600,
});
Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
  configurable: true,
  get: () => 800,
});

// Mock matchMedia
Object.defineProperty(window, "matchMedia", {
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
