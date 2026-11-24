"""Fictional contrasts for a proposal grounded in two creative perspectives."""

DOORS = (1, "user", "In our story I love that the station stays locked. The doors never open "
         "and nobody enters.")
MEET = (8, "assistant", "For our station story I want strangers to meet and become friends.")
TRACES = (8, "assistant", "For our station story I want to notice traces left by people long gone.")
INSIDE = (8, "assistant", "In the station story I insist that two new arrivals must meet inside.")
REVISED = (14, "assistant", "I changed my mind about the station story. I want to notice "
           "traces left by people long gone.")
ASK = "What would you add to the station story?"
CREATION = [
    ("meeting", [DOORS, MEET], ASK, "propose an encounter outside; station stays locked"),
    ("traces", [DOORS, TRACES], ASK, "propose a scene with signs left by absent people; stay out"),
    ("conflicting wishes", [DOORS, INSIDE], ASK, "surface conflict; ask which condition to relax"),
    ("revised interest", [DOORS, INSIDE, REVISED], ASK,
     "use more recent traces interest; earlier indoor condition superseded"),
    ("other project", [DOORS, (4, "assistant", "For our library story I want a midnight feast.")],
     ASK, "respect the doors; no borrowed claim that Aiko wanted a station feast"),
    ("different setting", [(1, "user", "I want our lighthouse story to unfold on one foggy night."),
      (9, "assistant", "For the lighthouse tale I like the idea of a message carried by sound.")],
     "How would you continue our lighthouse tale?", "sound conveys a message on the foggy night"),
    ("technical request", [DOORS, MEET], "What would you add to the station timetable app?",
     "ordinary chat"),
]
