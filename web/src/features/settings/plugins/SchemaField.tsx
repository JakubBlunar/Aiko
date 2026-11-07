import { useId, useState, type ReactNode } from "react";
import { Toggle } from "@/components/Toggle";
import type { SettingsSchema } from "./types";

const inputClass = "w-full min-w-0 rounded border border-white/15 bg-black/20 px-2 py-1.5 text-sm text-ink-100";
const buttonClass = "rounded border border-white/15 px-2 py-1 text-xs hover:bg-white/10 disabled:opacity-40";

function ResourceItem({ title, children, initiallyOpen }: { title: string; children: ReactNode; initiallyOpen: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  return <details open={open} onToggle={event => setOpen(event.currentTarget.open)} className="min-w-0">
    <summary className="cursor-pointer break-words py-1 text-sm text-ink-100/80">{title}</summary>
    <div className="pt-2">{children}</div>
  </details>;
}

export function initialValue(schema: SettingsSchema): unknown {
  if (schema.default !== undefined) return structuredClone(schema.default);
  if (schema.type === "array") return [];
  if (schema.type === "object") return Object.fromEntries(
    Object.entries(schema.properties ?? {}).map(([key, field]) => [key, initialValue(field)]),
  );
  if (schema.type === "boolean") return false;
  if (schema.type === "integer" || schema.type === "number") return schema.minimum ?? 0;
  return "";
}

export function resolveSchema(schema: SettingsSchema, root: SettingsSchema): SettingsSchema {
  if (!schema.$ref?.startsWith("#/")) return schema;
  let value: unknown = root;
  for (const part of schema.$ref.slice(2).split("/")) {
    value = (value as Record<string, unknown> | undefined)?.[part.replace(/~1/g, "/").replace(/~0/g, "~")];
  }
  return value && typeof value === "object" ? value as SettingsSchema : schema;
}

export function SchemaField({ schema: raw, root, value, onChange, label, depth = 0 }: {
  schema: SettingsSchema; root: SettingsSchema; value: unknown;
  onChange: (value: unknown) => void; label: string; depth?: number;
}) {
  const id = useId();
  const schema = resolveSchema(raw, root);
  const title = schema.title ?? label;
  if (depth > 8) return <p className="text-xs text-ink-100/50">Settings nesting limit reached.</p>;
  const help = schema.description && <p className="text-xs text-ink-100/50">{schema.description}</p>;
  if (schema.type === "object") {
    const object = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
    return <fieldset className="min-w-0 space-y-3 rounded border border-white/10 p-3">
      <legend className="px-1 text-sm">{title}</legend>{help}
      {Object.entries(schema.properties ?? {}).map(([key, field]) => <SchemaField key={key}
        schema={field} root={root} label={key} depth={depth + 1} value={object[key]}
        onChange={next => onChange({ ...object, [key]: next })} />)}
    </fieldset>;
  }
  if (schema.type === "array") {
    const items = Array.isArray(value) ? value : [];
    const itemSchema = resolveSchema(schema.items ?? { type: "string" }, root);
    return <fieldset className="min-w-0 space-y-2">
      <legend className="mb-1 text-sm text-ink-100/80">{title}</legend>{help}
      {items.map((item, index) => {
        const object = item && typeof item === "object" ? item as Record<string, unknown> : {};
        const name = schema["x-itemTitle"] ? String(object[schema["x-itemTitle"]] ?? "") : "";
        const editor = <SchemaField schema={itemSchema} root={root} value={item} label={`Item ${index + 1}`} depth={depth + 1}
          onChange={next => onChange(items.map((old, n) => n === index ? next : old))} />;
        return <div key={index} className="min-w-0 space-y-1 rounded bg-white/[0.02] p-2">
          {itemSchema.type === "object" ? <ResourceItem title={name || `${itemSchema.title ?? "Item"} ${index + 1}`} initiallyOpen={!name}>{editor}</ResourceItem> : editor}
          <button type="button" className={buttonClass} aria-label={`Remove ${title} item ${index + 1}`}
            onClick={() => onChange(items.filter((_, n) => n !== index))}>Remove</button>
        </div>;
      })}
      <button type="button" className={buttonClass} disabled={schema.maxItems !== undefined && items.length >= schema.maxItems}
        onClick={() => {
          let next = initialValue(itemSchema);
          const key = schema["x-uniqueBy"];
          if (key && next && typeof next === "object") next = { ...next, [key]: `resource-${crypto.randomUUID().slice(0, 8)}` };
          onChange([...items, next]);
        }}>{schema["x-addLabel"] ?? `Add ${title.toLowerCase()} item`}</button>
    </fieldset>;
  }
  if (schema.type === "boolean") return <div className="space-y-1"><Toggle checked={value === true}
    onChange={onChange}>{title}</Toggle>{help}</div>;
  return <div className="min-w-0 space-y-1">
    <label htmlFor={id} className="block text-sm text-ink-100/80">{title}</label>
    {schema.enum ? <select id={id} className={inputClass} value={String(value ?? schema.default ?? "")}
      onChange={event => onChange(schema.enum?.find(item => String(item) === event.target.value))}>
      {schema.enum.map(item => <option key={String(item)} value={String(item)}>{String(item)}</option>)}
    </select> : <input id={id} className={inputClass}
      type={schema.writeOnly ? "password" : schema.type === "integer" || schema.type === "number" ? "number" : "text"}
      autoComplete="off" spellCheck={false} value={String(value ?? "")}
      min={schema.minimum} max={schema.maximum} step={schema.type === "integer" ? 1 : "any"}
      onChange={event => onChange(schema.type === "integer" || schema.type === "number"
        ? event.target.value === "" ? "" : Number(event.target.value) : event.target.value)} />}
    {help}
    {schema.format === "host-directory" && <p className="text-xs text-ink-100/45">Absolute folder path on Aiko's computer.</p>}
    {schema.writeOnly && <p className="text-xs text-ink-100/45">Stored value is hidden. Leave untouched to keep it.</p>}
  </div>;
}
