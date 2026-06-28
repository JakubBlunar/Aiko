import type { ReactNode } from "react";

/**
 * A checkbox wrapped in its label, normalising the
 * ``onChange={(e) => set(e.target.checked)}`` boilerplate to a plain
 * ``onChange(checked)``. The wrapper is ``flex items-center`` plus
 * whatever spacing / type tone the call site passes via ``className``
 * (e.g. ``gap-1`` for the dense panel filters, ``gap-2 text-xs
 * text-ink-100/70`` for the settings toggles). The label text / node is
 * the ``children`` so call sites keep full control of its markup.
 */
export function CheckboxField({
  checked,
  onChange,
  children,
  className = "",
  inputClassName = "",
  disabled = false,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  children: ReactNode;
  className?: string;
  inputClassName?: string;
  disabled?: boolean;
}) {
  return (
    <label className={`flex items-center ${className}`.trim()}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className={inputClassName || undefined}
      />
      {children}
    </label>
  );
}
