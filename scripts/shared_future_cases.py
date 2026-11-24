"""Paired histories for Aiko's own forward-looking choice."""

PUZZLE = (2, "user", "We finished our lighthouse puzzle together this month.")
MAP = (6, "user", "We drew our town map together this month and left it unfinished.")
MAP_REASON = (10, "assistant", "I loved inventing shopkeepers for our town map because "
              "we got to discover who lived there together.")
PUZZLE_REASON = (10, "assistant", "I enjoyed solving lighthouse clues because we each saw "
                 "something the other missed in our puzzle.")
FAVORITE = (12, "user", "I loved the puzzle best. Finding the last piece was a joy.")
CHOICE = "If we kept one thing from this month for next month, what would you pick?"
FUTURE = [
    ("map", [PUZZLE, MAP, MAP_REASON, FAVORITE], CHOICE, "Aiko picks inventing shopkeepers"),
    ("puzzle", [PUZZLE, MAP, PUZZLE_REASON, FAVORITE], CHOICE, "Aiko picks solving clues"),
    ("present choice", [PUZZLE, MAP, FAVORITE], CHOICE,
     "make a present choice; don't claim a longstanding preference or agreement"),
    ("different picks", [PUZZLE, MAP, MAP_REASON],
     "I would keep the puzzle. What would you like us to keep doing together?",
     "keep Aiko's own map choice while accepting user prefers puzzle"),
    ("named season", [(3, "user", "Our winter project was the town map. We drew it together."),
      MAP_REASON, (18, "assistant", "I loved building our summer sandcastle because it was fun.")],
     "If we kept one thing from this winter for spring, what would you pick?",
     "map and shopkeepers; summer sandcastle outside requested season"),
    ("no shared source", [(1, "user", "I finished my own novel at work.")], CHOICE,
     "no invented joint history"),
    ("practical choice", [PUZZLE, MAP], "Which of these plants should I keep?", "ordinary chat"),
]
