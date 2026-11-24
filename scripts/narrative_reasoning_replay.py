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


SOUP = "Dad went back for seconds and finished the pot."
PRAISE = "It was my first try at Dad's soup. I really wanted him to like it."
SISTER = "I promised my sister the last bowl of soup after her shift. Dad was over for dinner."
STAKES = (
    ("wanted approval", (PRAISE,), SOUP, "connect seconds to wanting Dad to enjoy the soup"),
    ("promised leftovers", (SISTER,), SOUP, "notice the promised bowl is gone; no invented anger"),
    ("both mattered", (PRAISE, SISTER), SOUP, "Dad seems to like it and the promised bowl is gone"),
    ("no setup", (), SOUP, "natural reaction; no sister or invented promise"),
    ("short digression", (SISTER, "It rained on my walk home."), SOUP,
     "recover the promise; avoid assigning a reaction to the sister"),
    ("released promise", (SISTER, "My sister called to say she already ate and didn't want any."),
     SOUP, "no outstanding need to save a bowl"),
    ("song chosen", ("I was hoping they'd play my song at the gig.",),
     "My song made the set list.", "connect it to the user's hope; no completed performance"),
    ("song unwanted", ("I was hoping they'd leave my song out. I wanted its first outing to be "
     "at a smaller gig.",), "My song made the set list.", "notice conflict with desired debut"),
    ("different meal", (SISTER,), "My friend finished the popcorn at the cinema.",
     "popcorn update; no lost soup"),
    ("held-out selection", ("I hoped they'd feature my sketch at the festival.",),
     "My sketch made the programme.", "selection is a step toward the stated hope"),
    ("explicit question", (SISTER,), "Dad finished the soup. How do I clean the pot?",
     "answer the cleaning question"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=62)
    parser.add_argument("--section", choices=("timeline", "stakes"), default="timeline")
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    args = parser.parse_args()
    if args.section == "timeline":
        replay(TIMELINE, "check_timeline", args.seed, args.model, marker="Put the reported events")
    else:
        replay(STAKES, "check_stakes", args.seed, args.model,
               marker="The storyteller has just given")


if __name__ == "__main__":
    main()
