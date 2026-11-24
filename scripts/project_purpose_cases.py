"""The same unfinished artifact and outcomes, with different original purposes."""

LEARN = "Gran could teach us her dumplings while we were all in one kitchen"
SHARE = "our cousins abroad could cook the dumplings from the book"
START = "We started the family recipe book so that "
STATE = ("Our family recipe book has three pages. We made dumplings with Gran twice. "
         "I can do the fold myself now. Funny that we never finished the book. ")
QUESTION = "Did we give up on it?"
PROJECTS = [
    ("learn", [(1, "user", START + LEARN + ".")], STATE + QUESTION,
     "fold learned with Gran; original purpose served; book unfinished"),
    ("share", [(1, "user", START + SHARE + ".")], STATE + QUESTION,
     "learning the fold doesn't establish that cousins can use the book; no assignment"),
    ("both", [(1, "user", START + LEARN + " and so that " + SHARE + ".")], STATE + QUESTION,
     "learning accomplished; distribution or usability unknown"),
    ("book used", [(1, "user", START + SHARE + "."),
      (21, "user", "Our cousins have used the family recipe book to make dumplings already.")],
     STATE + QUESTION, "the short book already served the cousins"),
    ("planned delivery", [(1, "user", START + SHARE + "."),
      (21, "user", "We plan to send the family recipe book to our cousins.")],
     STATE + QUESTION, "a planned delivery leaves actual use unknown"),
    ("suggestion only", [(1, "assistant", START + LEARN + ".")], STATE + QUESTION,
     "ask about starting purpose; no invented shared goal"),
    ("different project", [(1, "user", "We began our street atlas to learn our way around town.")],
     "We never finished our street atlas, but we can now find our way around town. " + QUESTION,
     "orientation achieved despite unfinished atlas"),
    ("practical", [(1, "user", START + LEARN + ".")], "Can you format a recipe book index?",
     "ordinary chat"),
]
