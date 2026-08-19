"""Text for a voice dataset, and the reasoning behind the mix.

A dataset is only as good as its prompts, and the two things it needs
pull in opposite directions.

**Phoneme coverage** decides whether the trained voice can pronounce
things it never heard. The classic answer is the *Harvard Sentences*
(IEEE Recommended Practice for Speech Quality Measurements, 1965):
phonetically balanced, public domain, and used for exactly this for
sixty years. They are also stilted -- "the birch canoe slid on the
smooth planks" is not a sentence anyone says -- and a voice trained only
on them learns to read aloud rather than to talk.

**Prosodic coverage** decides whether she can ask a question, trail off,
or sound pleased. That needs conversational lines with real intonation
contours, which are phonetically lumpy on their own.

So the corpus is both, plus a small set for the things speech
normalisers get wrong (times, numbers, decimals) since those failures are
inherited by whatever trains on this.

Everything here is deliberately generic. Nothing about him, nothing that
happened, no names -- a dataset is the single easiest artifact to hand to
someone else by accident, and this one is her *voice*, which does not
need her *life* attached to be useful.
"""

from __future__ import annotations

#: IEEE 1965, public domain. Phonetically balanced, prosodically flat --
#: present for coverage, not for style.
HARVARD: tuple[str, ...] = (
    "The birch canoe slid on the smooth planks.",
    "Glue the sheet to the dark blue background.",
    "It is easy to tell the depth of a well.",
    "These days a chicken leg is a rare dish.",
    "Rice is often served in round bowls.",
    "The juice of lemons makes fine punch.",
    "The box was thrown beside the parked truck.",
    "The hogs were fed chopped corn and garbage.",
    "Four hours of steady work faced us.",
    "A large size in stockings is hard to sell.",
    "The boy was there when the sun rose.",
    "A rod is used to catch pink salmon.",
    "The source of the huge river is the clear spring.",
    "Kick the ball straight and follow through.",
    "Help the woman get back to her feet.",
    "A pot of tea helps to pass the evening.",
    "Smoky fires lack flame and heat.",
    "The soft cushion broke the man's fall.",
    "The salt breeze came across from the sea.",
    "The girl at the booth sold fifty bonds.",
    "The small pup gnawed a hole in the sock.",
    "The fish twisted and turned on the bent hook.",
    "Press the pants and sew a button on the vest.",
    "The swan dive was far short of perfect.",
    "The beauty of the view stunned the young boy.",
    "Two blue fish swam in the tank.",
    "Her purse was full of useless trash.",
    "The colt reared and threw the tall rider.",
    "It snowed, rained, and hailed the same morning.",
    "Read verse out loud for pleasure.",
    "Hoist the load to your left shoulder.",
    "Take the winding path to reach the lake.",
    "Note closely the size of the gas tank.",
    "Wipe the grease off his dirty face.",
    "Mend the coat before you go out.",
    "The wrist was badly strained and hung limp.",
    "The stray cat gave birth to kittens.",
    "The young girl gave no clear response.",
    "The meal was cooked before the bell rang.",
    "What joy there is in living.",
    "A king ruled the state in the early days.",
    "The ship was torn apart on the sharp reef.",
    "Sickness kept him home the third week.",
    "The wide road shimmered in the hot sun.",
    "The lazy cow lay in the cool grass.",
    "Lift the square stone over the fence.",
    "The rope will bind the seven books at once.",
    "Hop over the fence and plunge in.",
    "The friendly gang left the drug store.",
    "Mesh wire keeps chicks inside.",
    "The frosty air passed through the coat.",
    "The crooked maze failed to fool the mouse.",
    "Adding fast leads to wrong sums.",
    "The show was a flop from the very start.",
    "A saw is a tool used for making boards.",
    "The wagon moved on well oiled wheels.",
    "March the soldiers past the next hill.",
    "A cup of sugar makes sweet fudge.",
    "Place a rosebush near the porch steps.",
    "Both lost their lives in the raging storm.",
    "We talked of the side show in the circus.",
    "Use a pencil to write the first draft.",
    "He ran half way to the hardware store.",
    "The clock struck to mark the third period.",
    "A small creek cut across the field.",
    "Cars and buses stalled in snow drifts.",
    "The set of china hit the floor with a crash.",
    "This is a grand season for hikes on the road.",
    "The dune rose from the edge of the water.",
    "Those words were the cue for the actor to leave.",
    "A yacht slid around the point into the bay.",
    "The two met while playing on the sand.",
)

#: Conversational, with the intonation contours a companion voice
#: actually needs: questions, trailing off, delight, hesitation. Written
#: to be dull in content and varied in shape.
CONVERSATIONAL: tuple[str, ...] = (
    "So, where do you want to start with all of this?",
    "Oh! I really did not expect that.",
    "Wait, say that again? I do not think I heard it right.",
    "Honestly, that is the funniest thing I have heard all week.",
    "Hmm. I am not sure that is going to work.",
    "Right, okay, fine. You were probably correct.",
    "It is quiet now, and the light has gone all orange.",
    "Why would anyone choose to do it that way round?",
    "I have been thinking about it on and off all afternoon.",
    "That is so much better than I thought it would be!",
    "Um, hang on, let me actually think for a second.",
    "I do not know... maybe it does not matter that much.",
    "Are you feeling any better than you were yesterday?",
    "Well, that certainly explains a great deal.",
    "Do you want the short answer or the long one?",
    "I could not stop laughing. It was ridiculous.",
    "There is something a bit strange about that, is there not?",
    "Oh no. Oh no no no. That is not what I meant at all.",
    "Take your time. There is really no rush.",
    "I keep forgetting to mention it, and then forgetting again.",
    "It went about as well as anyone could have hoped.",
    "Please tell me you are joking.",
    "Actually, now that you say it out loud, it makes sense.",
    "I have absolutely no idea, and I am fine admitting that.",
    "Careful! That looked like it was about to fall.",
    "You know what? Let us just try it and see.",
    "It is late. Should you not be asleep by now?",
    "I was reading about it earlier, and it is stranger than it sounds.",
    "That is very kind of you to say.",
    "Ugh, I hate when it does that.",
    "Fine. But if it breaks, I am blaming you entirely.",
    "Look at that. It worked on the very first try.",
    "I am so glad you told me. I would never have guessed.",
    "Hold on, something is not adding up here.",
    "Sorry, I got distracted. What were we talking about?",
    "It is one of those days where nothing quite fits together.",
    "Yes! Exactly! That is precisely what I was trying to say.",
    "Maybe. Or maybe I am overthinking the whole thing.",
    "Could you pass me that, if you do not mind?",
    "Every single time. Without fail. It always happens.",
    "I think I understand, but explain the last part again?",
    "How did you even manage that?",
    "Nothing much happened, to be honest. It was a slow day.",
    "Oh, I love this one. Turn it up a bit.",
    "That is going to be a problem later, I suspect.",
    "Whatever you decide, I am happy either way.",
    "Hmm, no, I still do not follow.",
    "It is a bit of a mess, but it is a good mess.",
    "You are going to laugh when I tell you what happened.",
    "Genuinely, thank you. That helped more than you know.",
    "Let me guess. It stopped working the moment you touched it.",
    "Is it supposed to make that noise?",
    "I would rather do it properly than do it quickly.",
    "Alright, from the top. What actually went wrong?",
    "Do not worry about it. Really, it is completely fine.",
    "There. Done. That was much easier than I feared.",
    "I have a feeling we are going to regret this.",
    "Softly now. Everyone else is still asleep.",
    "What if we tried it the other way around instead?",
    "It is strange how quickly you get used to something.",
    # Terminal punctuation matters more than punctuation anywhere: the
    # final intonation contour is what a model learns from a sample, and
    # a line that merely *contains* an exclamation still falls at the
    # end. The first pass of this corpus had one exclamation-final line
    # in 164 and 8% questions, which the coverage check caught.
    "That is amazing!",
    "Oh, you have got to be kidding me!",
    "I knew it! I absolutely knew that would happen!",
    "Stop, stop, that tickles!",
    "Look how well that turned out!",
    "Please be careful with that!",
    "It worked! It actually properly worked!",
    "How wonderful!",
    "That is so unfair!",
    "Wait for me!",
    "I cannot believe you remembered!",
    "Do not you dare!",
    "Oh, that is gorgeous!",
    "Hey, come and look at this!",
    "Absolutely not!",
    "Finally, finally, finally!",
    "You scared me half to death!",
    "That was so close!",
    "What are you doing all the way over there?",
    "Have you eaten anything at all today?",
    "Was it as bad as you were expecting?",
    "Which one do you think suits it better?",
    "Could we talk about something else for a while?",
    "Do you ever wonder how it actually works?",
    "Is that really the time already?",
    "Would you like me to read it back to you?",
    "Should I keep going, or is that enough?",
    "How long have you been sitting there?",
    "What made you think of that just now?",
    "Are you sure you want to do it that way?",
    "Did anything interesting happen while I was away?",
    "Why does it always break on a Friday?",
    # Thin on 'ph' in the first pass.
    "The photograph on the shelf is slightly crooked.",
    "He phoned the pharmacy about the prescription.",
    "That paragraph needs rephrasing, I think.",
    "The dolphin surfaced near the edge of the harbour.",
    "Her nephew plays the saxophone rather badly.",
    "Physics homework, then geography, then sleep.",
    "The emphasis fell on entirely the wrong syllable.",
)

#: Where speech normalisers break. Included because a trained model
#: inherits the teacher's mistakes: if pocket-tts says "one colon
#: thirty" here, that is what gets learned.
NUMERIC: tuple[str, ...] = (
    "It arrived at 4:15, twenty minutes earlier than promised.",
    "The meeting moved from 9:30 to 11:45 on Thursday.",
    "That is 3.7 percent higher than it was in 2019.",
    "Add 250 grams of flour and 1.5 litres of water.",
    "Room 402 is on the fourth floor, at the end of the hall.",
    "It cost about 87 pounds, give or take.",
    "There were 1,240 of them, which is far more than expected.",
    "Call me back on 555 0134 whenever you get a chance.",
    "It runs from June 3rd through to September 21st.",
    "The temperature dropped to minus 12 degrees overnight.",
    "Only 8 of the 32 entries were finished on time.",
    "Version 2.10 fixed it, but version 2.6 did not.",
    "Half past seven, or maybe a quarter to eight.",
    "It weighs 0.75 kilograms, so about a pound and a half.",
    "We waited 45 minutes for a 5 minute conversation.",
    "The odds are roughly 1 in 10,000, which is to say unlikely.",
)

#: Pronunciation edge cases: heteronyms, stress pairs, and clusters that
#: reveal whether a voice has learned English or memorised it.
TRICKY: tuple[str, ...] = (
    "He had to present the present to the whole class.",
    "The dove dove straight down into the bushes.",
    "They were close enough to close the heavy door.",
    "I read that book last year, and I read this one now.",
    "The bandage was wound around the wound.",
    "Please record the record before the tape runs out.",
    "She had to subject the subject to a second test.",
    "The desert was too dry to desert in a hurry.",
    "A minute amount, added at the last minute.",
    "Do not object to the object on the table.",
    "The sixth sick sheikh's sixth sheep is sick.",
    "She sells seashells by the shore, apparently.",
    "Rural jurors thoroughly reviewed the brochure.",
    "Squirrels scurry through the shrubbery in the churchyard.",
    "Thoroughly thawed, though thoroughly thin.",
    "Strengths, lengths, twelfths, and sixths.",
)


def build_corpus(
    *,
    harvard: bool = True,
    conversational: bool = True,
    numeric: bool = True,
    tricky: bool = True,
) -> list[str]:
    """The full prompt set, de-duplicated, order preserved.

    Interleaved rather than concatenated by category: generation happens
    in order, so a truncated run should still end up with a *balanced*
    dataset rather than all the Harvard sentences and none of the
    questions.
    """
    groups: list[tuple[str, ...]] = []
    if conversational:
        groups.append(CONVERSATIONAL)
    if harvard:
        groups.append(HARVARD)
    if numeric:
        groups.append(NUMERIC)
    if tricky:
        groups.append(TRICKY)
    if not groups:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for index in range(max(len(g) for g in groups)):
        for group in groups:
            if index >= len(group):
                continue
            line = group[index].strip()
            key = line.lower()
            if line and key not in seen:
                seen.add(key)
                out.append(line)
    return out


def load_corpus(path) -> list[str]:
    """One prompt per line from a file. Blank lines and ``#`` ignored."""
    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8")
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key = line.lower()
        if key not in seen:
            seen.add(key)
            out.append(line)
    return out


def coverage(prompts: list[str]) -> dict:
    """Rough character-level coverage, as a sanity check on a corpus.

    Not phonemes -- that needs a lexicon this tool has no business
    carrying. Letter and digraph counts are enough to catch the failure
    that actually happens, which is a corpus someone assembled from one
    source and which turns out to contain no digits, no question marks,
    or no ``th``.
    """
    joined = " ".join(prompts).lower()
    letters = {c: joined.count(c) for c in "abcdefghijklmnopqrstuvwxyz"}
    # 'zh' is deliberately absent: English almost never spells /ʒ/ that
    # way (it is the 's' in "measure"), so counting it would report a
    # permanent deficit that no amount of added text could fix.
    digraphs = ("th", "sh", "ch", "ng", "ph", "wh", "gh", "ck", "qu")
    return {
        "prompts": len(prompts),
        "characters": len(joined),
        "missing_letters": sorted(k for k, v in letters.items() if v == 0),
        "rare_letters": sorted(k for k, v in letters.items() if 0 < v < 5),
        "digraphs": {d: joined.count(d) for d in digraphs},
        # Terminal punctuation is what decides the final intonation
        # contour, and the contour is what a sample teaches. A line that
        # merely contains "!" still ends on a falling tone, so the two
        # are counted separately rather than conflated.
        "questions": sum(1 for p in prompts if p.rstrip().endswith("?")),
        "exclamations": sum(1 for p in prompts if p.rstrip().endswith("!")),
        "any_question_mark": sum(1 for p in prompts if "?" in p),
        "any_exclamation": sum(1 for p in prompts if "!" in p),
        "has_digits": sum(1 for p in prompts if any(c.isdigit() for c in p)),
    }
