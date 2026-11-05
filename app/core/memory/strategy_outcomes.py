"""Outcome wording shared by strategy schema offering and literal admission.

These are language signals, not a judgment that an approach caused a result.
The strategy validator still requires user testimony, goal, conditions and sources.
"""
import re

SUCCESS = re.compile(
    r"\b(?:worked(?!\s+up\b)|helped|fixed|solved|succeeded|successful)\b|"
    r"\bnow\s+(?:it|this|that)(?:\s+is|['\u2019]s|\s+s)\s+"
    r"(?:working|fine(?!\s+(?:to|if|when)\b))\b|"
    r"\b(?:it|this|that)\s+(?:works|is\s+working)\s+now\b", re.I,
)
FAILURE = re.compile(
    r"\b(?:failed|didn['\u2019]?t work|did not work|didn['\u2019]?t help|did not help|"
    r"still (?:fails|failing|broken))\b", re.I,
)


def outcome_schema_needed(rows):
    return any(row.role == "user" and (SUCCESS.search(row.content) or FAILURE.search(row.content))
               for row in rows)
