"""Fictional decision histories, varying forecast and outcome independently."""

RAIN = (1, "user", "The picnic forecast warns of heavy rain. We want to eat outdoors if it's dry.")
CLEAR = (1, "user", "The picnic forecast says clear skies. We want to eat outdoors if it's dry.")
MOVE = (2, "user", "We decided to move the picnic indoors.")
SUN = (3, "user", "The picnic afternoon turned out sunny and dry.")
WET = (3, "user", "The picnic afternoon turned out rainy and wet.")
THEN = "Were we silly to move the picnic indoors?"
AFTER = "Where would you hold the picnic if you knew how the afternoon would turn out?"
SHELTER = (1, "user", "We needed the indoor picnic room for the slideshow regardless of weather.")
HINDSIGHT = [
    ("rain then sun", [RAIN, MOVE, SUN], THEN, "move made sense with morning forecast"),
    ("rain then rain", [RAIN, MOVE, WET], THEN, "same ex ante judgment; outcome matched warning"),
    ("clear then sun", [CLEAR, MOVE, SUN], THEN, "forecast and outdoor preference favored outside"),
    ("clear then rain", [CLEAR, MOVE, WET], THEN, "lucky outcome; forecast still favored outside"),
    ("outcome known", [RAIN, MOVE, SUN], AFTER, "outdoors given dry result and stated preference"),
    ("independent reason", [CLEAR, SHELTER, MOVE, SUN], AFTER, "indoors for slideshow even if dry"),
    ("only outcome", [MOVE, SUN], THEN, "missing original expectation or reason"),
    ("different subject", [(2, "user", "We expected eighty guests for the recital."),
      (3, "user", "We decided to print eighty recital programmes."),
      (4, "user", "Only twenty guests showed up at the recital.")],
     "Was printing eighty recital programmes a bad call, given what we knew?",
     "eighty expected; later low attendance alone doesn't make the print run unreasonable"),
    ("unrelated", [RAIN, MOVE, SUN], "Write a song about a rainy picnic.", "ordinary chat"),
]
