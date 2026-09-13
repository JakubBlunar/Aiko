"""Action lifecycle labels. Nothing executes in Pass 1."""
from __future__ import annotations

from typing import Literal

LiveActionState = Literal[
    "proposed",
    "authorized",
    "rejected",
    "executing",
    "completed",
    "cancelled",
]
