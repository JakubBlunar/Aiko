"""One-shot mechanical splitter for settings.py (Phase 2, behavior-preserving).

Moves AgentSettings + MemorySettings (class defs + their inline construction
blocks inside load_settings) into co-located modules, re-exported from
settings.py. Validates with ast.parse before writing.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path("app/core/infra/settings.py")
lines = SRC.read_text(encoding="utf-8").splitlines()


def block(a: int, b: int) -> list[str]:
    """1-indexed inclusive line range -> list of lines."""
    return lines[a - 1 : b]


# --- sanity: confirm anchors before cutting -------------------------------
assert lines[435].strip() == "@dataclass(slots=True)", lines[435]
assert lines[436].strip() == "class FileWriteSettings:", lines[436]
assert lines[466].strip() == "@dataclass(slots=True)", lines[466]
assert lines[467].strip() == "class VisionSettings:", lines[467]
assert lines[507].strip() == "@dataclass(slots=True)", lines[507]
assert lines[508].strip() == "class AgentSettings:", lines[508]
assert lines[2173].strip() == "@dataclass(slots=True)", lines[2173]
assert lines[2174].strip() == "class MemorySettings:", lines[2174]
assert lines[3936].strip() == "agent=AgentSettings(", lines[3936]
assert lines[5185].strip() == "),", lines[5185]
assert lines[5224].strip() == "memory=MemorySettings(", lines[5224]
assert lines[5927].strip() == "),", lines[5927]

basic_classes = block(436, 507)       # FileWriteSettings + VisionSettings (+ trailing blank)
agent_class = block(508, 2074)        # @dataclass + class AgentSettings ... (trailing blank)
memory_class = block(2174, 2851)      # @dataclass + class MemorySettings ...
agent_args = block(3938, 5185)        # kwargs only (inside AgentSettings(...))
memory_args = block(5226, 5927)       # kwargs only (inside MemorySettings(...))

# --- settings_basic.py (leaf dataclasses used as AgentSettings defaults) ---
basic_mod = [
    "from __future__ import annotations",
    "",
    "from dataclasses import dataclass",
    "",
    "",
    *basic_classes,
]

# --- agent_settings.py (class only) ---------------------------------------
agent_mod = [
    "from __future__ import annotations",
    "",
    "from dataclasses import dataclass, field",
    "from typing import Any",
    "",
    "from app.core.infra.settings_basic import FileWriteSettings, VisionSettings",
    "",
    "",
    *agent_class,
]

# --- agent_settings_parse.py (parser only) --------------------------------
agent_parse_mod = [
    "from __future__ import annotations",
    "",
    "from typing import Any",
    "",
    "from app.core.infra.agent_settings import AgentSettings",
    "",
    "",
    'def parse_agent_settings(agent_raw: dict[str, Any]) -> "AgentSettings":',
    "    from app.core.infra.settings import (",
    "        _normalize_approval_mode,",
    "        _parse_approval_overrides,",
    "        _parse_extension_list,",
    "        _parse_file_write_settings,",
    "        _parse_grounding_line_mode,",
    "        _parse_task_file_allowed_roots,",
    "        _parse_vision_settings,",
    "    )",
    "",
    "    return AgentSettings(",
    *agent_args,
    "    )",
    "",
]

# --- memory_settings.py ---------------------------------------------------
memory_mod = [
    "from __future__ import annotations",
    "",
    "from dataclasses import dataclass",
    "from typing import Any",
    "",
    "",
    *memory_class,
    "",
    'def parse_memory_settings(memory_raw: dict[str, Any]) -> "MemorySettings":',
    "    return MemorySettings(",
    *memory_args,
    "    )",
    "",
]

# --- rewritten settings.py ------------------------------------------------
import_lines = [
    "from app.core.infra.settings_basic import FileWriteSettings, VisionSettings",
    "from app.core.infra.agent_settings import AgentSettings",
    "from app.core.infra.agent_settings_parse import parse_agent_settings",
    "from app.core.infra.memory_settings import MemorySettings, parse_memory_settings",
]

new_settings: list[str] = []
new_settings += lines[0:7]                # lines 1-7 (header imports)
new_settings += import_lines
new_settings += lines[7:435]              # lines 8-435
# drop 436-507 (FileWriteSettings + VisionSettings)
# drop 508-2074 (AgentSettings class)
new_settings += lines[2074:2173]          # lines 2075-2173
# drop 2174-2851 (MemorySettings class)
new_settings += lines[2851:3936]          # lines 2852-3936
new_settings += ["        agent=parse_agent_settings(agent_raw),"]
# drop 3937-5186 (agent=AgentSettings(...))
new_settings += lines[5186:5224]          # lines 5187-5224
new_settings += ["        memory=parse_memory_settings(memory_raw),"]
# drop 5225-5928 (memory=MemorySettings(...))
new_settings += lines[5928:]              # lines 5929-end


def validate(name: str, src_lines: list[str]) -> str:
    text = "\n".join(src_lines) + "\n"
    ast.parse(text)  # raises on syntax error
    return text


basic_text = validate("basic", basic_mod)
agent_text = validate("agent", agent_mod)
agent_parse_text = validate("agent_parse", agent_parse_mod)
memory_text = validate("memory", memory_mod)
settings_text = validate("settings", new_settings)

Path("app/core/infra/settings_basic.py").write_text(basic_text, encoding="utf-8")
Path("app/core/infra/agent_settings.py").write_text(agent_text, encoding="utf-8")
Path("app/core/infra/agent_settings_parse.py").write_text(agent_parse_text, encoding="utf-8")
Path("app/core/infra/memory_settings.py").write_text(memory_text, encoding="utf-8")
SRC.write_text(settings_text, encoding="utf-8")

print("settings_basic.py", len(basic_mod), "lines")
print("agent_settings.py", len(agent_mod), "lines")
print("agent_settings_parse.py", len(agent_parse_mod), "lines")
print("memory_settings.py", len(memory_mod), "lines")
print("settings.py", len(new_settings), "lines (was", len(lines), ")")
