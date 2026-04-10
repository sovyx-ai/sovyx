import { describe, it, expect, vi, beforeEach } from "vitest";
import { createStatsSlice, type StatsSlice } from "./stats";

// Minimal mock of StateCreator args
function createSlice(): StatsSlice {
  let state: Partial<StatsSlice> = {};
  const set = (partial: Partial<StatsSlice>) => {
    state = { ...state, ...partial };
  };
  const get = () => state as StatsSlice;
  const slice = createStatsSlice(
    set as Parameters<typeof createStatsSlice>[0],
    get as Parameters<typeof createStatsSlice>[1],
    { setState: set, getState: get, subscribe: vi.fn(), getInitialState: get } as Parameters<typeof createStatsSlice>[2],
  );
  // Merge initial state
  state = { ...slice };
  return state as StatsSlice;
}

describe("StatsSlice", () => {
  let slice: StatsSlice;

  beforeEach(() => {
    slice = createSlice();
  });

  it("has correct initial state", () => {
    expect(slice.statsHistory).toEqual([]);
    expect(slice.statsTotals).toBeNull();
    expect(slice.statsMonth).toBeNull();
    expect(slice.statsLoading).toBe(false);
    expect(slice.statsError).toBeNull();
  });

  it("fetchStatsHistory is a function", () => {
    expect(typeof slice.fetchStatsHistory).toBe("function");
  });
});
