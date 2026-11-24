"""A turn-local reasoning hint and, when all conditions are understood, its answer."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PracticalAnswer:
    guidance: str = ""
    reply: str = ""
