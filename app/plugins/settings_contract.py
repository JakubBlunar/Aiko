"""Plugin-owned declarative settings shared by discovery, SDK and UI; stdlib only."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PluginSettings:
    schema: dict[str, Any] = field(default_factory=dict)
    ui: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_manifest(cls, root: Path, manifest: dict) -> "PluginSettings":
        declaration = manifest.get("settings") or {}
        if not isinstance(declaration, dict):
            raise ValueError("settings must reference schema/ui JSON files")

        def read(name):
            relative = declaration.get(name)
            if relative is None:
                return {}
            if not isinstance(relative, str) or ":" in relative or Path(relative).is_absolute():
                raise ValueError("settings metadata paths must be bundle-relative")
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root.resolve()):
                raise ValueError("settings metadata cannot escape its plugin bundle")
            raw = path.read_bytes()
            if len(raw) > 65536:
                raise ValueError("settings metadata exceeds 64 KiB")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("settings metadata must contain an object")
            return value

        schema, ui = read("schema"), read("ui")

        def check(value):
            if isinstance(value, dict):
                if "$ref" in value and not str(value["$ref"]).startswith("#/"):
                    raise ValueError("settings schemas support local fragment references only")
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(schema)
        result = cls(schema=schema, ui=ui)

        def fields(node, depth=0):
            if depth > 12 or not isinstance(node, dict):
                raise ValueError("Settings fields require bounded object schemas")
            node = result.resolve(node)
            for key in ("title", "description"):
                if key in node and not isinstance(node[key], str):
                    raise ValueError(f"Settings {key} must be text")
            if "enum" in node and (
                not isinstance(node["enum"], list)
                or any(isinstance(value, (dict, list)) for value in node["enum"])
            ):
                raise ValueError("Settings enums must list scalar choices")
            properties = node.get("properties", {})
            if not isinstance(properties, dict):
                raise ValueError("Settings properties must be an object")
            for child in properties.values():
                fields(child, depth + 1)
            if "items" in node:
                fields(node["items"], depth + 1)

        fields(schema)
        for key in ("advanced", "required_for_activation", "resources"):
            if key in ui and not isinstance(ui[key], list):
                raise ValueError(f"settings UI {key} must be an array")
        if any(not isinstance(item, dict) for item in ui.get("resources", [])):
            raise ValueError("settings resources must contain objects")
        for resource in ui.get("resources", []):
            for key in ("field", "name_field", "path_field", "scope_field", "mode"):
                if key in resource and not isinstance(resource[key], str):
                    raise ValueError(f"Resource {key} must be text")
        for key in ("advanced", "required_for_activation"):
            if any(not isinstance(item, str) for item in ui.get(key, [])):
                raise ValueError(f"settings UI {key} must name fields")
        if "description" in ui and not isinstance(ui["description"], str):
            raise ValueError("settings UI description must be text")
        if "legacy_single" in ui and not isinstance(ui["legacy_single"], dict):
            raise ValueError("legacy_single must contain migration rules")
        return result

    def resolve(self, schema):
        seen = set()
        while "$ref" in schema:
            ref = schema["$ref"]
            if ref in seen:
                raise ValueError("Cyclic settings reference")
            seen.add(ref)
            schema = self.schema
            for key in ref[2:].split("/"):
                schema = schema[key.replace("~1", "/").replace("~0", "~")]
        return schema

    def merge_config(self, base: dict, patch: dict) -> dict:
        """Merge drafts, preserving omitted write-only fields inside resource rows."""

        def merge(old, new, schema):
            schema = self.resolve(schema)
            if isinstance(old, dict) and isinstance(new, dict):
                out = deepcopy(old)
                for key, value in new.items():
                    out[key] = merge(old.get(key), value, schema.get("properties", {}).get(key, {}))
                return out
            if isinstance(old, list) and isinstance(new, list):
                item_schema = schema.get("items", {})
                unique = schema.get("x-uniqueBy")
                out = []
                for index, item in enumerate(new):
                    previous = (
                        next(
                            (
                                row
                                for row in old
                                if isinstance(row, dict)
                                and isinstance(item, dict)
                                and row.get(unique) == item.get(unique)
                            ),
                            None,
                        )
                        if unique
                        else (old[index] if index < len(old) else None)
                    )
                    # Only carry omitted secrets. Other removed fields really are removed.
                    visible = deepcopy(item)
                    if isinstance(previous, dict) and isinstance(visible, dict):
                        for key, field in self.resolve(item_schema).get("properties", {}).items():
                            if (
                                self.resolve(field).get("writeOnly")
                                and key not in visible
                                and key in previous
                            ):
                                visible[key] = deepcopy(previous[key])
                    out.append(visible)
                return out
            return deepcopy(new)

        return merge(base, patch, self.schema)

    def normalize(self, config: dict, explicit: set[str]) -> dict:
        """Optional plugin-declared legacy single-value to resource-list migration."""
        result = deepcopy(config)
        for target, rule in self.ui.get("legacy_single", {}).items():
            if target in explicit or not isinstance(rule, dict):
                continue
            source = result.get(rule.get("from"))
            if not source:
                continue
            template = rule.get("item")
            if isinstance(template, dict):
                item = {
                    key: deepcopy(result.get(value["from"], value.get("default")))
                    if isinstance(value, dict) and "from" in value
                    else deepcopy(value)
                    for key, value in template.items()
                }
            else:
                item = deepcopy(source)
            result[target] = [item]
        return result

    def public_config(self, config: dict) -> dict:
        """Expose declared fields only; unknown legacy keys and secrets stay local."""

        def project(value, schema):
            schema = self.resolve(schema)
            if isinstance(value, dict):
                return {
                    key: project(value[key], spec)
                    for key, spec in schema.get("properties", {}).items()
                    if key in value and not self.resolve(spec).get("writeOnly")
                }
            if isinstance(value, list):
                return [project(item, schema.get("items", {})) for item in value]
            return deepcopy(value)

        return project(config, self.schema)
