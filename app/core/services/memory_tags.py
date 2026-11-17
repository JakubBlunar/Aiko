"""Shared invisible remember-tag grammar, including quoted JSON note bodies."""
import json
import re


REMEMBER_TAG_RE = re.compile(
    r"\[\[remember(?::(?P<kind>self))?:(?P<body>\{(?:(?!\]\]).)+?|[^\]]+?)\]\]",
    flags=re.IGNORECASE | re.DOTALL,
)
REMEMBER_OPEN_TAIL_RE = re.compile(
    r"\[\[remember:(?:self:)?(?:\{(?:(?!\]\]).)*|[^\]]*)\Z",
    flags=re.IGNORECASE | re.DOTALL,
)


def note_content(body: str) -> str:
    """Classifiers should see the chosen note, not its quotation or JSON syntax."""
    if body.lstrip().startswith("{"):
        try:
            value = json.loads(body)
            if isinstance(value, dict) and isinstance(value.get("content"), str):
                return value["content"]
        except (TypeError, ValueError):
            pass
    return body
