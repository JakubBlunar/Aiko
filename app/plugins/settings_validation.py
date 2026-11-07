"""Host validation of plugin-owned schemas; no plugin imports or network resolution."""

from __future__ import annotations

from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from app.plugins.settings_contract import PluginSettings


def validate_settings(spec: PluginSettings, config: dict) -> list[dict]:
    checker = FormatChecker()

    @checker.checks("host-directory")
    def directory(value):
        if not isinstance(value, str) or not value:
            return True
        return Path(value).is_absolute() and Path(value).is_dir()

    if not spec.schema:
        return []
    try:
        Draft202012Validator.check_schema(spec.schema)
        validator = Draft202012Validator(spec.schema, format_checker=checker, registry=Registry())
        errors = []
        for error in validator.iter_errors(config):
            node, secret = spec.schema, False
            for key in error.absolute_path:
                node = spec.resolve(node)
                secret = secret or node.get("writeOnly", False)
                node = (
                    node.get("items", {})
                    if isinstance(key, int)
                    else (node.get("properties", {}).get(key, {}))
                )
            secret = secret or spec.resolve(node).get("writeOnly", False)
            errors.append(
                {
                    "path": list(error.absolute_path),
                    "message": f"Invalid secret setting ({error.validator})"
                    if secret
                    else error.message[:300],
                }
            )

        def unique(value, schema, path):
            schema = spec.resolve(schema)
            if isinstance(value, dict):
                for key, child in value.items():
                    unique(child, schema.get("properties", {}).get(key, {}), [*path, key])
            elif isinstance(value, list):
                key = schema.get("x-uniqueBy")
                if key:
                    ids = [item.get(key) for item in value if isinstance(item, dict)]
                    if len(set(ids)) != len(ids):
                        errors.append({"path": path, "message": f"Each {key} must be unique"})
                for index, child in enumerate(value):
                    unique(child, schema.get("items", {}), [*path, index])

        unique(config, spec.schema, [])
        return errors[:20]
    except Exception as exc:
        return [{"path": [], "message": f"Invalid settings definition: {exc}"[:300]}]


def activation_problem(spec: PluginSettings, config: dict) -> str:
    errors = validate_settings(spec, config)
    if errors:
        return errors[0]["message"]
    missing = [key for key in spec.ui.get("required_for_activation", []) if not config.get(key)]
    return "Configure " + ", ".join(missing) + " before enabling tools" if missing else ""
