"""Human names for completed planner actions that can support a voluntary pursuit."""

import re


# The evidence rules are shared. Group executor keys only when they describe
# the same activity; housekeeping actions are not evidence of a hobby.
ACTIVITY_BY_KEY = {
    "home_new_sketch": "sketching", "home_revise_sketch": "sketching",
    "read_book": "reading", "writing": "writing", "garden": "tending plants",
    "tea": "making tea", "stretching": "stretching",
}
ALIASES = {name: name for name in ACTIVITY_BY_KEY.values()}
ALIASES.update({"drawing": "sketching", "gardening": "tending plants", "reading books": "reading"})
_NAMES = "|".join(re.escape(name) for name in sorted(ALIASES, key=len, reverse=True))
ACTIVITY_WORDS = re.compile(r"\b(?:" + _NAMES + r")\b", re.I)


def activity_mentions(text):
    return {ALIASES[m.group().lower()] for m in ACTIVITY_WORDS.finditer(text)}


def pursuit_label(activity):
    return f"I keep coming back to {activity} in my own time"


LABEL_ACTIVITIES = {pursuit_label(activity): activity for activity in ACTIVITY_BY_KEY.values()}
