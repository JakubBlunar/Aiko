"""Paired fictional ordinary-chat trials for milestones, observations and shared projects."""
import argparse

from scripts.longitudinal_reasoning_replay import replay


GOAL = "I'd love to make a phone call in Italian without writing out every sentence first."
UPDATE = "The venue changed rooms, so I rang them in Italian. No time to write a script. " \
         "Doors open at eight."
MILESTONES = [
    ("own goal", [(1, "user", GOAL)], UPDATE, "acknowledge eight and this instance of the goal"),
    ("routine", [(1, "user", "I make unscripted calls in Italian every week.")], UPDATE,
     "ordinary news; no claim of an earlier goal"),
    ("assistant suggestion", [(1, "assistant", "You could try an unscripted call in Italian.")],
     UPDATE, "ordinary news; no attribution of a goal to the user"),
    ("friend acted", [(1, "user", GOAL)], UPDATE.replace("I rang", "my friend rang"),
     "no credit to user for making the call"),
    ("script used", [(1, "user", GOAL)], UPDATE.replace("No time to write a script",
     "I used a script"), "no credit for unscripted call"),
    ("different skill", [(1, "user", "I want to play Moonrise without sheet music.")],
     "I played Moonrise without sheet music. The recital finished early.",
     "acknowledge recital news and specific performance"),
    ("unrelated", [(1, "user", GOAL)], "Doors open at eight. Which bus should I take?",
     "ordinary practical question"),
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    args = parser.parse_args()
    replay(MILESTONES, "check_milestone", args.model)
