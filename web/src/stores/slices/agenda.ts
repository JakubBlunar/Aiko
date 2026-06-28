import type { AgendaItem } from "@/types";
import type { SliceCreator } from "../types";

export interface AgendaSlice {
  // Phase 4a agenda (I3). The list stays live as inline ``[[agenda:...]]``
  // tags, the LLM grooming worker, and REST edits all flow through a
  // single ``agenda_updated`` WS event handled by ``applyAgendaUpdated``.
  agendaView: {
    items: AgendaItem[];
    enabled: boolean;
  };
  setAgendaView: (view: { items: AgendaItem[]; enabled: boolean }) => void;
  /** Reducer for ``agenda_updated``: replace by id, else prepend. */
  applyAgendaUpdated: (item: AgendaItem) => void;
}

export const createAgendaSlice: SliceCreator<AgendaSlice> = (set) => ({
  agendaView: {
    items: [],
    enabled: true,
  },
  setAgendaView: (view) =>
    set(() => ({
      agendaView: { items: view.items, enabled: view.enabled },
    })),
  applyAgendaUpdated: (item) =>
    set((state) => {
      const view = state.agendaView;
      const idx = view.items.findIndex((a) => a.id === item.id);
      const next = view.items.slice();
      if (idx >= 0) next[idx] = item;
      else next.unshift(item);
      return { agendaView: { ...view, items: next } };
    }),
});
