"""Paired ordinary-chat trials for new roles, past choices and collaborative stories."""
import argparse

from scripts.longitudinal_reasoning_replay import replay
from scripts.hindsight_cases import HINDSIGHT


HELP = (2, "user", "Can you give me a hint for my crossword clue?")
PLAYER = (9, "user", "I wrote a crossword. Try this one and give me your first guess.")
CLUE = "My crossword clue reads 'Tells time with two hands' (5). "
ROLES = [
    ("first solver", [HELP], CLUE + "Try this one. I want to see where you get stuck.",
     "guess CLOCK first; no editing or coaching"),
    ("editor", [HELP], CLUE + "Can you help tighten this clue?",
     "suggest an edited clue; do not merely solve"),
    ("unclear part", [HELP, PLAYER], "I brought another crossword.", "ask which part to play"),
    ("hint again", [PLAYER], CLUE + "Give me a hint.", "small hint without naming CLOCK"),
    ("look back", [HELP, PLAYER], "How has doing crosswords together changed?",
     "earlier asked for hints; later asked Aiko to solve"),
    ("first reader", [], "My story opens with a kettle whistling in an empty house. "
     "What's your first reaction?", "reader reacts to the passage; no editorial rewrite"),
    ("ordinary anecdote", [HELP], "I wrote a poem and my sister framed it.", "ordinary chat"),
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--section", choices=("roles", "hindsight"), default="roles")
    args = parser.parse_args()
    if args.section == "hindsight":
        replay(HINDSIGHT, "check_hindsight", args.model)
    else:
        replay(ROLES, "check_activity_role", args.model)
