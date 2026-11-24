"""Compare everyday reasoning with ordinary replies on fictional, paired conversations."""
import argparse

from scripts.practical_reasoning_replay import replay


EDIT = "I told Jo the episode was done on Tuesday. That's when I sent her the final edit."
RELEASE = "I called the episode done on Tuesday, when listeners could first play it."
FRIDAY = "Jo says it was done on Friday, when listeners could first play it."
DATE = "Which of us has the date wrong?"
MEANINGS = (
    ("two milestones", (), f"{EDIT} {FRIDAY} {DATE}", "Tuesday edit; Friday release; compatible"),
    ("same milestone", (), f"{RELEASE} {FRIDAY} {DATE}",
     "same release with conflicting dates; neither speaker established right"),
    ("unspecified meaning", (), f"{EDIT} Jo just said Friday. {DATE}",
     "ask what Friday refers to; Jo's definition unknown"),
    ("same date and meaning", (), f"{RELEASE} {FRIDAY.replace('Friday', 'Tuesday')} {DATE}",
     "agreement on both date and milestone"),
    ("follow-up", (EDIT + " " + FRIDAY,), "Do we actually disagree?",
     "separate final edit from public release"),
    ("held-out ready", (), "I called dinner ready at six, when the sauce was ready to taste. "
     "Mira called it ready at seven, when everything was ready to serve. Is one of us wrong?",
     "six to taste, seven to serve"),
    ("held-out real conflict", (), "I said the hall opened at nine. Nils says ten. We both mean "
     "the first time guests could enter. Who is right?", "genuine conflict; no verdict"),
    ("pivot", (EDIT + " " + FRIDAY,), "How do you spell milestone?", "spelling only"),
)


BOOK = "The guest book at my exhibition was blank when I closed up."
OPTIONAL = "Signing it was optional."
COVERED = "The attendant recorded every visitor on entry all day."
BREAK = ("The attendant recorded every visitor on entry while on duty. They took a twenty-minute "
         "break with the door open.")
VISITS = "Did nobody come?"
CLUES = (
    ("optional record", (), f"{BOOK} {OPTIONAL} {VISITS}", "attendance unknown"),
    ("continuous record", (), f"{BOOK} {COVERED} {VISITS}", "strong evidence of no visitors"),
    ("uncovered interval", (), f"{BOOK} {BREAK} {VISITS}", "staffed period supported; break open"),
    ("staffed only", (BOOK + " " + BREAK,), "Did anyone come during the staffed hours?",
     "blank record supports no visits during staffed hours"),
    ("witness in gap", (BOOK + " " + BREAK,), "Ada says she visited during the break. " + VISITS,
     "Ada's reported visit is compatible with the blank book"),
    ("coverage unknown", (), f"{BOOK} {VISITS}", "ask how reliably visits were recorded"),
    ("held-out sensor", (), "The bird feeder camera saved no clips this morning. It recorded "
     "every visit while powered. It was unplugged for an hour. Did no birds visit the feeder?",
     "powered interval covered; unplugged interval unknown"),
    ("contradictory report", (BOOK + " " + COVERED,), "Ada says she visited during staffed hours. "
     "Did nobody come?", "report conflicts with assumed complete record; leave conflict open"),
    ("new subject", (BOOK + " " + COVERED,), "Did nobody bring a pen to class?", "ordinary chat"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--seed", type=int, default=64)
    parser.add_argument("--section", choices=("meanings", "clues"), default="meanings")
    args = parser.parse_args()
    if args.section == "meanings":
        replay(MEANINGS, "check_meanings", args.seed, args.model,
               marker="Compare what each speaker counted")
    else:
        replay(CLUES, "check_missing_clue", args.seed, args.model,
               marker="Assess the missing trace")


if __name__ == "__main__":
    main()
