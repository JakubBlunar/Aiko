import {
  createContext, useContext, useId, useRef, useState,
  type ReactNode, type RefObject,
} from "react";

interface NavigationState {
  selected: Record<string, string>;
  select: (area: string, section: string) => void;
  scrollRoot: RefObject<HTMLDivElement>;
}
const Navigation = createContext<NavigationState | null>(null);

export function SettingsNavigationProvider({ children }: { children: ReactNode }) {
  const [selected, setSelected] = useState<Record<string, string>>({});
  const scrollRoot = useRef<HTMLDivElement>(null);
  return <Navigation.Provider value={{ selected, scrollRoot,
    select: (area, section) => {
      setSelected((previous) => ({ ...previous, [area]: section }));
      if (scrollRoot.current) scrollRoot.current.scrollTop = 0;
    },
  }}>{children}</Navigation.Provider>;
}

export function useSettingsScrollRoot() {
  return useContext(Navigation)?.scrollRoot;
}

export interface SettingsSubsection {
  id: string;
  label: string;
  content: ReactNode;
}

/** Keep forms mounted between subsection changes so drafts and consent dialogs survive. */
export function SettingsSections({ area, sections }: {
  area: string;
  sections: readonly SettingsSubsection[];
}) {
  const navigation = useContext(Navigation);
  const [local, setLocal] = useState(sections[0]?.id ?? "");
  const uid = useId();
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const preferred = navigation?.selected[area] ?? local;
  const selected = sections.some((section) => section.id === preferred) ? preferred : sections[0]?.id;
  const select = (id: string) => {
    setLocal(id);
    navigation?.select(area, id);
  };
  return <div className="space-y-5">
    <div className="sticky -top-5 z-10 -mx-5 -mt-5 border-b border-white/10 bg-[#0f0a1f]/95 px-5 py-3 backdrop-blur">
      <div role="tablist" aria-label={`${area} sections`} className="flex flex-wrap gap-1">
        {sections.map((section, index) => <button key={section.id} type="button" role="tab"
          id={`${uid}-${section.id}-tab`} aria-controls={`${uid}-${section.id}-panel`}
          aria-selected={selected === section.id} tabIndex={selected === section.id ? 0 : -1}
          ref={(element) => { buttons.current[index] = element; }} onClick={() => select(section.id)}
          onKeyDown={(event) => {
            const next = event.key === "ArrowRight" ? (index + 1) % sections.length
              : event.key === "ArrowLeft" ? (index + sections.length - 1) % sections.length
              : event.key === "Home" ? 0 : event.key === "End" ? sections.length - 1 : -1;
            if (next < 0) return;
            event.preventDefault();
            select(sections[next].id);
            buttons.current[next]?.focus();
          }}
          className={`rounded-md px-3 py-1.5 text-xs font-medium transition focus-visible:outline focus-visible:outline-ink-400 ${
            selected === section.id ? "bg-ink-500/30 text-ink-100 ring-1 ring-ink-400/50"
              : "text-ink-100/60 hover:bg-white/5 hover:text-ink-100"
          }`}>{section.label}</button>)}
      </div>
    </div>
    {sections.map((section) => <div key={section.id} role="tabpanel"
      id={`${uid}-${section.id}-panel`} aria-labelledby={`${uid}-${section.id}-tab`}
      hidden={selected !== section.id} className="space-y-5">
      {section.content}
    </div>)}
  </div>;
}
