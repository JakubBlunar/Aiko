"""Registry-wide guard: no ``demand()`` probe may mutate.

The per-worker tests each assert their own probe writes nothing. This
one asserts it for every worker at once, including ones added after
those tests were written — a probe runs on every scheduler tick whether
or not the worker is admitted, so a write inside one fires far more
often than the worker does and is invisible in normal use.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.audit_demand_probes import audit  # noqa: E402


class DemandProbeAuditTests(unittest.TestCase):
    def test_no_probe_writes(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent / "app" / "core"
        findings = audit(root)
        self.assertEqual(
            findings,
            [],
            "demand() probes must not mutate; add a deliberate exception "
            "to ALLOW in scripts/audit_demand_probes.py if the write is "
            "a probe-local memo:\n" + "\n".join(findings),
        )


if __name__ == "__main__":
    unittest.main()
