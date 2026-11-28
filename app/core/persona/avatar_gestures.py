"""Capability bindings shared by the chat vocabulary and avatar dispatcher."""

_GESTURE_FLAGS = {
    "wink_left": "has_wink",
    "wink_right": "has_wink",
    "tail_wag_slow": "has_tail_wag",
    "tail_wag_fast": "has_tail_wag",
    "tail_flick": "has_tail_wag",
}


def overlay_capability(name: str) -> str:
    return _GESTURE_FLAGS.get(name, f"has_{name}")
