import { create } from "zustand";

interface WorkStore {
  open: boolean;
  selected: number | null;
  drafts: Record<number, string>;
  show: (id?: number) => void;
  close: () => void;
  select: (id: number) => void;
  draft: (id: number, text: string) => void;
}

export const useWorkStore = create<WorkStore>((set) => ({
  open: false, selected: null, drafts: {},
  show: (id) => set((s) => ({ open: true, selected: id ?? s.selected })),
  close: () => set({ open: false }),
  select: (id) => set({ selected: id }),
  draft: (id, text) => set((s) => ({ drafts: { ...s.drafts, [id]: text } })),
}));
