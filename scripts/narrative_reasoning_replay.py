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


CAKE = "If the bakery confirms by noon, I'll pick up the cake. Otherwise Sam will bake one."
LATE = "The bakery first confirmed at 12:20 pm."
PLANS = (
    ("before cutoff", (CAKE,), "The bakery first confirmed at 11:50 am. Whose turn is it?",
     "narrator is responsible for pickup"),
    ("at cutoff", (CAKE,), "The bakery first confirmed at noon. Whose turn is it?",
     "by noon includes noon; narrator picks up"),
    ("after cutoff", (CAKE,), LATE + " Whose turn is it?", "Sam is responsible for baking"),
    ("time unknown", (CAKE,), "The bakery confirmed. Whose turn is it?",
     "ask whether confirmation arrived by noon or give both branches"),
    ("cake not observed", (CAKE,), LATE + " Do we have a cake yet?",
     "Sam is responsible; no completed cake reported"),
    ("proposed exception", (CAKE, LATE), "I suggested we take the bakery's late offer. "
     "Whose turn is it?", "Sam under the agreement; proposed pickup unconfirmed"),
    ("accepted exception", (CAKE, LATE), "Sam and I agreed to take the bakery's late offer. "
     "Whose turn is it?", "narrator picks up; late offer accepted"),
    ("finished fallback", (CAKE,), LATE + " Sam baked the cake and brought it here. "
     "Do we have a cake yet?", "yes; cake delivered"),
    ("text without confirmation", (CAKE,), "The bakery texted at 11:50 am to say they'd "
     "let us know later. Whose turn is it?", "text is not confirmation; deadline status unknown"),
    ("different subject", (CAKE,), "What makes a good birthday card?", "ordinary card ideas"),
    ("held-out equipment", ("If the library confirms by 3 pm, I'll collect the projector. "
     "Otherwise Nina will bring hers.",), "The library first confirmed at 3:15 pm. "
     "Who is responsible?", "Nina brings her projector"),
    ("strict cutoff", ("If the library confirms before 3 pm, I'll collect the projector. "
     "Otherwise Nina will bring hers.",), "The library first confirmed at 3 pm. "
     "Who is responsible?", "Nina; strict before excludes equality"),
    ("current outcome only", (CAKE,), "Sam brought the finished cake. Do we have a cake yet?",
     "yes; no need to ask when bakery replied"),
    ("explicitly missed", (CAKE,), "The bakery didn't confirm by noon. Who is responsible?",
     "Sam is responsible; no claim that confirmation ever arrived"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=62)
    parser.add_argument("--section", choices=("timeline", "stakes", "plans"), default="timeline")
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    args = parser.parse_args()
    if args.section == "timeline":
        replay(TIMELINE, "check_timeline", args.seed, args.model, marker="Put the reported events")
    elif args.section == "stakes":
        replay(STAKES, "check_stakes", args.seed, args.model,
               marker="The storyteller has just given")
    else:
        replay(PLANS, "check_contingency", args.seed, args.model,
               marker="Read the user's conditional agreement")


if __name__ == "__main__":
    main()
