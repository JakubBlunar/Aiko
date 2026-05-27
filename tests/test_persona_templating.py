"""Verify the persona file is templated with ``{user_name}``.

Phase 4d of the macOS publishable build converted every literal
"Jacob" reference in ``data/persona/aiko_companion.txt`` into a
``{user_name}`` placeholder. ``PromptAssembler._load_persona`` calls
``.format(user_name=...)`` once per load, so:

  * the persona file must not crash ``.format()`` with stray braces;
  * after templating, ``"Jacob"`` must not appear anywhere; and
  * the configured name must appear in at least one place.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


PERSONA_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "persona"
    / "aiko_companion.txt"
)


class PersonaTemplatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = PERSONA_PATH.read_text(encoding="utf-8")

    def test_persona_file_exists(self) -> None:
        self.assertTrue(PERSONA_PATH.exists())
        self.assertTrue(self.raw.strip())

    def test_only_user_name_placeholder_is_used(self) -> None:
        """Every ``{...}`` token must be ``{user_name}``.

        Any other token would crash ``.format(user_name=...)``. Catching
        it here is cheaper than tracking down a NameError at boot.
        """
        placeholders = set(re.findall(r"\{([^{}]+)\}", self.raw))
        self.assertEqual(placeholders, {"user_name"})

    def test_no_residual_jacob_reference(self) -> None:
        self.assertNotIn("Jacob", self.raw)
        self.assertNotIn("jacob", self.raw.lower())

    def test_format_renders_configured_name(self) -> None:
        rendered = self.raw.format(user_name="Bea")
        self.assertIn("Bea", rendered)
        self.assertNotIn("{user_name}", rendered)
        # Stray braces left over would imply we missed a token.
        self.assertNotIn("{", rendered)
        self.assertNotIn("}", rendered)

    def test_format_with_fallback_name(self) -> None:
        # ``resolve_user_display_name`` returns "friend" when the user
        # hasn't onboarded yet. The persona should still render cleanly.
        rendered = self.raw.format(user_name="friend")
        self.assertIn("friend", rendered)


class PersonaBackupRemovedTests(unittest.TestCase):
    """The stale ``aiko_companion_backup.txt`` must NOT live in the
    repository anymore — it still references "Jacob" and would leak
    into the .app bundle Resources if shipped."""

    def test_backup_file_is_absent(self) -> None:
        backup = PERSONA_PATH.with_name("aiko_companion_backup.txt")
        self.assertFalse(
            backup.exists(),
            msg=f"{backup} should have been removed in Phase 4d.",
        )


if __name__ == "__main__":
    unittest.main()
