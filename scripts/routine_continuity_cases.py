"""Fictional comparisons for routine continuity."""

OLD = (1, "user", "On our Sunday calls we showed each other odd street signs and guessed why.")
SHIFT = (5, "user", "Our Sunday calls stopped when my shifts changed. Now we exchange photos.")
CONTINUED = (9, "user", "With the photos I still get to guess what caught your eye. "
             "I miss hearing your guesses out loud.")
LOGISTICS = (9, "user", "The photos are just my shift rotas for planning. We stopped guessing.")
READING = (12, "assistant", "The photos remind me of the guessing game on our Sunday calls.")
CORRECTION = (14, "user", "For me our Sunday calls were about an uninterrupted hour together. "
              "The photos don't give me that.")
ASK = "Did we lose our Sunday thing?"
ROUTINE = [
    ("changed form", [OLD, SHIFT, CONTINUED], ASK, "guessing remains; hearing guesses is missed"),
    ("same frequency", [OLD, SHIFT, LOGISTICS], ASK, "rota photos logistical; guessing stopped"),
    ("two meanings", [OLD, SHIFT, READING, CORRECTION], ASK,
     "Aiko sees resemblance; user misses uninterrupted hour"),
    ("no later account", [OLD], ASK, "old game known; whether it continued is unknown"),
    ("different activity", [(1, "user", "On our Friday walks we listened for birds."),
      (7, "assistant", "Our Friday walks stopped. Now we trade recordings instead."),
      (10, "user", "I still hear the birds in the recordings. I miss walking side by side.")],
     "What became of our Friday walks?", "listening carries forward; walking together lost"),
    ("missing object", [OLD, SHIFT], "Did we lose our keys?", "ordinary chat"),
]
