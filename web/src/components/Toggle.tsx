import type { ReactNode } from "react";
import { CheckboxField } from "./CheckboxField";

/**
 * The standard settings-row checkbox toggle: a checkbox + inline label in
 * the muted settings type tone (``flex items-center gap-2 text-xs
 * text-ink-100/70``). A thin wrapper over {@link CheckboxField} that bakes in
 * that shared styling so the settings tabs stop repeating it on every row.
 *
 * Pass ``className`` for per-row layout — typically a top margin (``mt-3``) or
 * an indent for a dependent sub-toggle (``ml-4``).
 */
export function Toggle({
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
    <CheckboxField
      checked={checked}
      onChange={onChange}
      disabled={disabled}
      inputClassName={inputClassName}
      className={`gap-2 text-xs text-ink-100/70 ${className}`.trim()}
    >
      {children}
    </CheckboxField>
  );
}
