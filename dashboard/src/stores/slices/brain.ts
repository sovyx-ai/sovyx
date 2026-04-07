import type { StateCreator } from "zustand";
import type { BrainGraph, BrainNode, BrainSearchResult } from "@/types/api";
import type { DashboardState } from "../dashboard";

export interface BrainSlice {
  brainGraph: BrainGraph | null;
  setBrainGraph: (g: BrainGraph) => void;
  selectedBrainNode: BrainNode | null;
  setSelectedBrainNode: (n: BrainNode | null) => void;
  brainSearchResults: BrainSearchResult[];
  setBrainSearchResults: (r: BrainSearchResult[]) => void;
  brainSearchQuery: string;
  setBrainSearchQuery: (q: string) => void;
}

export const createBrainSlice: StateCreator<
  DashboardState,
  [],
  [],
  BrainSlice
> = (set) => ({
  brainGraph: null,
  setBrainGraph: (g) => set({ brainGraph: g }),
  selectedBrainNode: null,
  setSelectedBrainNode: (n) => set({ selectedBrainNode: n }),
  brainSearchResults: [],
  setBrainSearchResults: (r) => set({ brainSearchResults: r }),
  brainSearchQuery: "",
  setBrainSearchQuery: (q) => set({ brainSearchQuery: q }),
});
