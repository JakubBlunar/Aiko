"""Tests for the reusable task-capability + approval framework."""
from __future__ import annotations

import unittest

from app.core.tasks.approval import (
    APPROVE,
    APPROVE_ALL,
    APPROVE_ALL_SCOPE,
    DENY,
    MODE_ASK,
    MODE_AUTO,
    build_request,
    normalize_mode,
    parse_decision,
    resolve_approval,
)
from app.core.tasks.capabilities import (
    CAPABILITY_FILE_WRITE,
    TaskCapability,
    all_capabilities,
    get_capability,
    register_capability,
)
from app.core.tasks.task_handler import TaskInputNeeded


class CapabilityRegistryTests(unittest.TestCase):
    def test_file_write_registered(self) -> None:
        cap = get_capability(CAPABILITY_FILE_WRITE)
        self.assertIsNotNone(cap)
        assert cap is not None
        self.assertTrue(cap.destructive)
        self.assertIn(cap, all_capabilities())

    def test_register_and_overwrite(self) -> None:
        register_capability(
            TaskCapability(id="unit_test_cap", label="do a test", destructive=False)
        )
        self.assertIsNotNone(get_capability("unit_test_cap"))
        # Overwrite wins (last write).
        register_capability(
            TaskCapability(id="unit_test_cap", label="do a test", destructive=True)
        )
        cap = get_capability("unit_test_cap")
        assert cap is not None
        self.assertTrue(cap.destructive)

    def test_register_requires_id(self) -> None:
        with self.assertRaises(ValueError):
            register_capability(TaskCapability(id="", label="x"))


class NormalizeModeTests(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(normalize_mode("ask"), MODE_ASK)
        self.assertEqual(normalize_mode("AUTO"), MODE_AUTO)
        self.assertEqual(normalize_mode(" auto "), MODE_AUTO)

    def test_invalid_falls_back(self) -> None:
        self.assertEqual(normalize_mode("nonsense"), MODE_ASK)
        self.assertEqual(normalize_mode(None), MODE_ASK)
        self.assertEqual(normalize_mode("", default=MODE_AUTO), MODE_AUTO)


class ResolveApprovalTests(unittest.TestCase):
    def test_global_ask_default(self) -> None:
        self.assertEqual(resolve_approval("file_write"), MODE_ASK)

    def test_global_auto(self) -> None:
        self.assertEqual(
            resolve_approval("file_write", mode="auto"), MODE_AUTO
        )

    def test_override_wins_over_global(self) -> None:
        self.assertEqual(
            resolve_approval(
                "file_write", mode="ask", overrides={"file_write": "auto"}
            ),
            MODE_AUTO,
        )
        self.assertEqual(
            resolve_approval(
                "file_write", mode="auto", overrides={"file_write": "ask"}
            ),
            MODE_ASK,
        )

    def test_override_for_other_capability_ignored(self) -> None:
        self.assertEqual(
            resolve_approval(
                "file_write", mode="ask", overrides={"shell_exec": "auto"}
            ),
            MODE_ASK,
        )

    def test_session_capability_approve_all(self) -> None:
        self.assertEqual(
            resolve_approval(
                "file_write", mode="ask", session_approved={"file_write"}
            ),
            MODE_AUTO,
        )

    def test_session_all_scope_approves_everything(self) -> None:
        self.assertEqual(
            resolve_approval(
                "anything", mode="ask", session_approved={APPROVE_ALL_SCOPE}
            ),
            MODE_AUTO,
        )

    def test_session_beats_override_ask(self) -> None:
        # Even an explicit ask override is trumped by a session approve-all.
        self.assertEqual(
            resolve_approval(
                "file_write",
                mode="ask",
                overrides={"file_write": "ask"},
                session_approved={"file_write"},
            ),
            MODE_AUTO,
        )


class BuildRequestTests(unittest.TestCase):
    def test_builds_input_needed_with_options(self) -> None:
        cap = get_capability(CAPABILITY_FILE_WRITE)
        assert cap is not None
        req = build_request(cap, "overwrite Notes:todo.md")
        self.assertIsInstance(req, TaskInputNeeded)
        self.assertIn("overwrite Notes:todo.md", req.prompt)
        self.assertEqual(req.options, [APPROVE, APPROVE_ALL, DENY])


class ParseDecisionTests(unittest.TestCase):
    def test_exact_options(self) -> None:
        self.assertEqual(parse_decision("approve"), APPROVE)
        self.assertEqual(parse_decision("approve all"), APPROVE_ALL)
        self.assertEqual(parse_decision("deny"), DENY)

    def test_empty_is_deny(self) -> None:
        self.assertEqual(parse_decision(""), DENY)
        self.assertEqual(parse_decision("   "), DENY)

    def test_freetext_yes(self) -> None:
        self.assertEqual(parse_decision("yes"), APPROVE)
        self.assertEqual(parse_decision("sure, go ahead"), APPROVE)
        self.assertEqual(parse_decision("ok do it"), APPROVE)

    def test_freetext_no(self) -> None:
        self.assertEqual(parse_decision("no"), DENY)
        self.assertEqual(parse_decision("nope, cancel that"), DENY)
        self.assertEqual(parse_decision("don't"), DENY)

    def test_freetext_approve_all(self) -> None:
        self.assertEqual(parse_decision("yes to all"), APPROVE_ALL)
        self.assertEqual(parse_decision("approve everything"), APPROVE_ALL)
        self.assertEqual(parse_decision("always allow"), APPROVE_ALL)
        self.assertEqual(parse_decision("stop asking, approve"), APPROVE_ALL)

    def test_ambiguous_defaults_deny(self) -> None:
        self.assertEqual(parse_decision("hmm maybe"), DENY)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
