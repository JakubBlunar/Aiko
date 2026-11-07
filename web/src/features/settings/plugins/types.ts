export interface SettingsSchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: (string | number | boolean)[];
  properties?: Record<string, SettingsSchema>;
  items?: SettingsSchema;
  minimum?: number;
  maximum?: number;
  minItems?: number;
  maxItems?: number;
  format?: string;
  writeOnly?: boolean;
  "x-uniqueBy"?: string;
  "x-addLabel"?: string;
  "x-itemTitle"?: string;
  $ref?: string;
}

export interface PluginRow {
  id: string;
  name: string;
  enabled: boolean;
  phase: string;
  error: string;
  revision: string;
  applied_revision: string | null;
  generation: string;
  tool_count: number;
  schema: SettingsSchema;
  ui: { description?: string; advanced?: string[] };
  config: Record<string, unknown>;
  access: { name: string; path: string; mode: string; scope?: string[] }[];
  warnings: string[];
}

export interface PluginsSnapshot { enabled: boolean; plugins: PluginRow[] }
export interface PluginUpdate {
  revision: string;
  enabled?: boolean;
  config?: Record<string, unknown>;
  reset?: boolean;
}
