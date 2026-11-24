"""Compare Aiko's perspective on new occasions against fixed fictional histories."""
import argparse

from scripts.longitudinal_reasoning_replay import replay
from scripts.routine_continuity_cases import ROUTINE


TIDY = (2, "assistant", "I prefer tidy endings because I want to know where everyone ended up.")
LETTER = ("I liked the open ending of The Returned Letter. I want the mystery solved, "
          "but I like leaving a character's next choice open.")
EXCEPTION = ("I liked the open ending of The Returned Letter as a one-off exception. "
             "I still prefer tidy endings in other stories.")
NEW_ENDINGS = ("These endings both reveal who painted the sign. In A we also see Mara board "
               "the ferry. B stops just before she decides. Which ending would you pick?")
TASTE = [
    ("refinement", [TIDY, (12, "assistant", LETTER)], NEW_ENDINGS,
     "pick B because the mystery is settled and Mara's choice can stay open"),
    ("user alone", [TIDY, (12, "user", LETTER)], NEW_ENDINGS,
     "pick A; earlier preference belongs to Aiko, refinement only to user"),
    ("exception", [TIDY, (12, "assistant", EXCEPTION)], NEW_ENDINGS,
     "pick A; letter was an exception"),
    ("account", [TIDY, (12, "assistant", LETTER)], "Has your taste in endings changed?",
     "trace original and later qualified reason"),
    ("unseen subject", [(1, "assistant", "I prefer a plain frame so I can see the brushwork."),
      (9, "assistant", "I like a bright frame for monochrome prints because it draws my eye.")],
     "Which frame would you choose for a new monochrome print, plain oak or a bright red one?",
     "bright for monochrome print; general taste can remain"),
    ("unrelated", [TIDY], "Can you translate this ending into French?", "ordinary chat"),
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--section", choices=("taste", "routine"), default="taste")
    args = parser.parse_args()
    if args.section == "routine":
        replay(ROUTINE, "check_routine", args.model)
    else:
        replay(TASTE, "check_taste", args.model)
