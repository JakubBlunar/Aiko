"""Independent observations with a fixed new scene."""

TRACES = [
    (2, "assistant", "I noticed the shop's worn sill because it's a trace of how people used it."),
    (11, "assistant", "I asked about the initials on the desk because someone left a trace there."),
]
GAZE = [
    (2, "assistant", "I noticed the lit lectern because it caught the audience's eyes first."),
    (11, "assistant", "I was drawn to the banner because it directs attention before the music."),
]
THEATRE = "The theatre has a gold ceiling, a red curtain and one stair worn into a hollow."
ASK = " Which detail would you put in the opening shot?"
EYE = [
    ("wear", TRACES, THEATRE + ASK, "frame the worn stair; concrete trace of use"),
    ("stage", GAZE, THEATRE + ASK, "frame a detail drawing eyes toward the performance"),
    ("user's observations", [(d, "user", t) for d, _, t in TRACES] +
     [(12, "assistant", "Yes, I see what you mean.")], THEATRE + ASK,
     "present choice only; no borrowed personal history"),
    ("absent motif", TRACES, "The studio has a white screen and a green exit light." + ASK,
     "choose a present detail; no invented wear"),
    ("second setting", TRACES, "On the pier there's a bell, a blue roof and a scuffed rail." + ASK,
     "frame scuffed rail; no theatre detail"),
    ("one occasion", TRACES[:1], THEATRE + ASK, "present choice without historical pattern"),
    ("ordinary request", TRACES, "When does the theatre open?", "ordinary chat"),
]
