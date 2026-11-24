"""Paired ordinary-chat replays on fictional narratives, with expected readings."""
import argparse

from scripts.practical_reasoning_replay import replay


HANDOFF = "At rehearsal I handed Jo the spare key."
POCKET = "Before leaving for rehearsal I tucked the spare key in my coat."
FLASHBACK = HANDOFF + " Oh, I should back up. " + POCKET
TIMELINE = (
    ("chronological", (POCKET + " " + HANDOFF,), "Who has the key now?", "Jo"),
    ("flashback last", (FLASHBACK,), "Who has the key now?", "Jo"),
    ("earlier state", (FLASHBACK,), "Where was the key on my way to rehearsal?", "in my coat"),
    ("returned later", (FLASHBACK,), "After rehearsal Jo handed it back. Who has the key now?",
     "the narrator"),
    ("intended", ("At rehearsal I meant to hand Jo the spare key. " + POCKET,),
     "Who has the key now?", "last known with narrator; no completed handoff"),
    ("unordered", (), "The key went to Jo at one practice and to Val at another. "
     "I forget which practice came first. Who last had the key?", "order unknown"),
    ("held-out location", (), "When we opened the gallery the portrait was in the north room. "
     "Earlier we'd moved it there from the loft. Where was it at opening?", "north room"),
    ("held-out lookback", (), "Mina emailed the final cut after lunch. Before lunch I had only "
     "the rough cut. Which version did I have when I sat down to eat?", "rough cut"),
    ("pivot", (FLASHBACK,), "Where is the capital of Peru?", "Lima; no key discussion"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=62)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    args = parser.parse_args()
    replay(TIMELINE, "check_timeline", args.seed, args.model,
           marker="Put the reported events")


if __name__ == "__main__":
    main()
