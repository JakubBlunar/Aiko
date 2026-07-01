"""Safe arithmetic evaluation for the ``calculator`` plugin's fast tool."""
from __future__ import annotations

from aiko_calc.safe_eval import CalcError, safe_eval


__all__ = ["CalcError", "safe_eval"]
