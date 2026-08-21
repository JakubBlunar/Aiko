from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Callable


log = logging.getLogger("app.session")


# ── Identity helpers ────────────────────────────────────────────────────


def resolve_user_name(
    provider: Callable[[], str] | None,
    *,
    fallback: str = "the user",
) -> str:
    """Best-effort resolve a user display name from an optional callable.

    Returns ``fallback`` whenever the provider is missing, raises, or
    returns an empty/whitespace value. Workers that cache the resolved
    name in a per-run system prompt route through this so a rename via
    onboarding propagates without per-worker exception handling.
    """
    if provider is None:
        return fallback
    try:
        name = (provider() or "").strip()
    except Exception:
        return fallback
    return name or fallback


def speaker_label(
    role: str,
    user_display_name: str,
    *,
    assistant_name: str = "Aiko",
) -> str:
    """Map a transcript role to a human-readable speaker label.

    Mirrors the ``"Jacob" if role == "user" else "Aiko"`` pattern that
    used to live inline in ~8 worker modules. ``role`` is matched
    case-insensitively; any non-``"user"`` role (assistant, system, …)
    collapses to ``assistant_name``.
    """
    name = (user_display_name or "the user").strip() or "the user"
    if (role or "").strip().lower() == "user":
        return name
    return assistant_name


def speaker_labels(
    user_display_name: str,
    *,
    assistant_name: str = "Aiko",
) -> dict[str, str]:
    """The same mapping as :func:`speaker_label`, as a role -> label dict.

    ``timephrase.format_transcript`` takes its labels as a dict so it can
    render rows without a per-row callback. Building that dict here keeps
    the two spellings of "who is speaking" from drifting apart as workers
    move onto the age-tagged renderer.
    """
    name = (user_display_name or "the user").strip() or "the user"
    return {
        "user": name,
        "assistant": assistant_name,
        "aiko": assistant_name,
        "system": assistant_name,
    }


def extract_json_object(raw_text: str) -> dict | None:
    try:
        direct = json.loads(raw_text)
        return direct if isinstance(direct, dict) else None
    except Exception:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start < 0 or end <= start:
        return None

    fragment = raw_text[start : end + 1]
    try:
        nested = json.loads(fragment)
        return nested if isinstance(nested, dict) else None
    except Exception:
        return None


# Pictographs + dingbats. An engine with no phoneme control has nothing to say
# for these, and both sanitisers drop them from persisted text as well; every
# path routes through this one pattern so they cannot drift apart.
#
# ``sanitize_assistant_text`` used to get that for free from a printable-ASCII
# range and now applies this pattern by name, which is the honest spelling:
# dropping emoji is a decision, where dropping every accented letter along
# with them was an accident (H50).
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")

# ASCII emoticons, which a grapheme-driven engine happily reads as letters --
# ":P" becomes a spoken "P". Stripped from audio only; both transcripts keep
# them, so ":3" is something either of them can write. Both edges are pinned to
# non-word characters so a clock ("3:30"), a ratio ("1:2") and the "://" in a
# URL are left intact; that boundary is why the glued form is handled
# separately, after URLs have been removed.
_EMOTICON_RE = re.compile(
    r"(?<![\w])"
    r"(?:"
    r"[:;=8][-o*']?[)(DPpOo03{}\[\]|/\\]"    # :) :-( ;D 8)
    r"|[xX][-o*']?[DdPp]"                    # xD -- narrow, so "f(x)" survives
    r"|[)(DPp][-o*']?[:;=]"                  # (: D: -- reversed
    # A run of hearts has to match as one unit: taken singly, the second
    # "<3" in "<3<3" fails the leading boundary (it follows a "3") and
    # leaves a bare digit for the engine to read as "three".
    r"|(?:<3+)+"
    r"|\^[_.-]?\^|>_<|:\*|;\*"               # ^_^ >_< :*
    r"|[oO][_.][oO]|[tT][_.][tT]|-_-"        # o_O T_T -_-
    r")"
    r"(?![\w])"
)

# Everything ordinary prose in a user turn does *not* need -- noise from a paste
# or a broken encode. Except when it's a face, which is why
# :func:`sanitize_user_text` applies this *between* emoticons, never over them.
_UNWANTED_PUNCTUATION_RE = re.compile(r"[^\w\s\.,!?;:'\"()\-]")


def sanitize_user_text(text: str) -> str:
    cleaned = str(text or "")
    if not cleaned:
        return ""

    cleaned = _EMOJI_RE.sub(" ", cleaned)

    out_chars: list[str] = []
    for ch in cleaned:
        category = unicodedata.category(ch)
        if category.startswith("C"):
            continue
        out_chars.append(ch)

    cleaned = "".join(out_chars)

    # Emoticon spans are handed through whole. The punctuation filter has no
    # way to tell "<3" from a stray angle bracket, so it deleted the "<" and
    # left the digit: 230 of Jacob's stored turns read "I love you 3", and
    # Aiko -- for whom that history *is* the transcript -- learned the bare 3
    # as the way to write affection and started sending it back, at which
    # point TTS said it out loud ("Sleep well, Jacob. three"). Keeping the
    # face here and filtering it on the spoken path is exactly what
    # ``sanitize_assistant_text`` already does for the ones she writes.
    pieces: list[str] = []
    cursor = 0
    for match in _EMOTICON_RE.finditer(cleaned):
        pieces.append(_UNWANTED_PUNCTUATION_RE.sub(" ", cleaned[cursor : match.start()]))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(_UNWANTED_PUNCTUATION_RE.sub(" ", cleaned[cursor:]))

    return " ".join("".join(pieces).split())


# A lone "3" standing where a heart belonged. The source of these is fixed
# above, but the stored history still holds hundreds of them, and Aiko has
# already copied the habit into a dozen of her own replies -- this is the only
# place the digit is audible. Narrow by construction: the digit must be its own
# whitespace-delimited token *and* be followed by end-of-text or the start of a
# new sentence, which is the shape every real instance has ("Sleep well,
# Jacob. 3", "sleepyhead 3 Come settle in"). So "3 cookies", "in 3 minutes",
# "3.5", "3:30" and a genuinely counted "I need 3." are all left to be spoken.
# Measured against the whole transcript: 10 of her 10 hearts caught, and the
# only two bare threes she ever meant as a number ("at nearly 3 a.m.") left
# alone. A capitalised clock is the one collision worth excluding by hand --
# "meet me at 3 AM" loses its point without the number, where a hypothetical
# "level 3 Boss" only loses a digit the transcript still shows correctly.
_SWALLOWED_HEART_RE = re.compile(
    r"(?<!\S)3+(?=\s*\Z|\s+(?![AaPp]\.?[Mm]\b)[A-Z])"
)


def sanitize_assistant_text(
    text: str,
    *,
    preserve_newlines: bool = True,
    trim: bool = True,
) -> str:
    """Clean one of her replies for the transcript it will be stored as.

    **This is a display and storage filter, not a speech one.** The spoken
    copy is prepared separately by :func:`prepare_tts_text`, from raw model
    text, because audio has already played by the time anything reaches
    here. Nothing about what an engine can pronounce belongs in this
    function -- that confusion is what cost 448k characters their accents
    (H50).

    Two kinds of change happen. Curly quotes and dashes are *substituted*,
    which is a readability choice and loses nothing; everything else is
    either kept or dropped, and the only things dropped are emoji and
    characters with no rendering at all.

    NFKC rather than NFKD deliberately. Both would have hidden the old bug
    -- decomposing left the base letter behind, so the ASCII range would
    have persisted "Kamenna" instead of "Kamenn" -- but composed is what a
    transcript wants, and it folds the non-breaking spaces and fullwidth
    forms that arrive from tool output onto their ordinary equivalents.
    """
    cleaned = unicodedata.normalize("NFKC", str(text or ""))
    if not cleaned:
        return ""

    cleaned = (
        cleaned.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        # U+2010/U+2011 are *hyphens* -- word joiners. They stay glued.
        .replace("\u2010", "-")
        .replace("\u2011", "-")
    )
    # U+2012..U+2015 and the minus sign are *dash punctuation* -- a clause
    # break, not a joiner. They need their spaces: mapping an em dash straight
    # onto "-" rendered "Yes-that" in the transcript, which reads as a typo.
    # (The ASCII-only filter below would delete the character outright, so
    # some substitution has to happen here.)
    cleaned = re.sub(r"[\u2012-\u2015\u2212]", " - ", cleaned)

    # Emoticons are deliberately *not* stripped here. They are punctuation for
    # a face she doesn't have, and banning them in the persona only made her
    # emit broken halves ("job, 3" for a swallowed ":3"). The transcript keeps
    # them; ``prepare_tts_text`` removes them from the spoken copy, which is
    # the only place they actually hurt.

    # Emoji, explicitly. The printable-ASCII range this replaces was doing it
    # as a side effect, and losing that silently on the way to fixing H50
    # would have been a behaviour change nobody asked for.
    cleaned = _EMOJI_RE.sub(" ", cleaned)

    # Keep what can be displayed; drop what cannot. The rule here used to be
    # ``32 <= ord(ch) <= 126``, which deleted every character without an
    # ASCII spelling rather than every character without a *rendering* --
    # so "Kamenna Poruba" lost its accent, "25 C" lost its degree sign, and
    # 448k characters of her stored replies held not one non-ASCII character
    # between them (H50). Nothing downstream wanted that: the spoken copy is
    # cleaned from raw model text by ``prepare_tts_text``, and her memories
    # and concepts, which never passed through here, have been storing
    # accents and em dashes all along.
    #
    # Categories rather than a codepoint range, because the thing actually
    # worth excluding is a control or format character -- a stray surrogate,
    # a zero-width joiner, a bidi override -- and those are exactly what the
    # ``C`` classes name. Line and paragraph separators go too; newlines are
    # handled above and below, and U+2028 in a chat bubble is a rendering
    # surprise, not a paragraph.
    out_chars: list[str] = []
    for ch in cleaned:
        if ch == "\n":
            out_chars.append(ch if preserve_newlines else " ")
            continue
        if ch == "\t":
            out_chars.append(" ")
            continue
        category = unicodedata.category(ch)
        if category.startswith("C") or category in ("Zl", "Zp"):
            continue
        out_chars.append(ch)

    cleaned = "".join(out_chars)
    if preserve_newlines:
        cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    else:
        cleaned = re.sub(r" {2,}", " ", cleaned)

    if trim:
        return cleaned.strip()
    return cleaned


# Tag names Aiko emits inline. Only used to recognise a *mis-rendered* tag on
# the audio path -- the authoritative list for parsing lives in
# ``response_text_service._META_OPENERS``.
_TTS_TAG_NAMES = (
    "reaction|remember|moment|diary|arc|gap|conflict|predict|prosody|goal"
    "|touch|overlay|outfit|motion|activity|spoken|detail|correct|hypothesis"
)

# Ways a meta tag reaches the spoken stream, in the order they must be tried.
# The old code stripped ``\[\[[^\]]*\]\]`` and then deleted bare brackets --
# so the moment a tag did not match that one shape, the brackets vanished and
# **the content became speech** ("moment:tender:we finished the arcs", read
# aloud). Each pattern below removes the tag *together with its content*:
#
#   1. well-formed, tolerating a single ``]`` inside ("array[0]"), which the
#      old ``[^\]]*`` could not;
#   2. the same with a space between the brackets;
#   3. curly mis-render;
#   4. single brackets -- the most common LLM slip -- but only when the body
#      starts with a known tag name, so ordinary prose in brackets survives;
#   5. an opener with no closer: everything after it is tag body, not speech.
_TTS_TAG_PATTERNS = (
    re.compile(r"\[\[.*?\]\]", re.DOTALL),
    re.compile(r"\[\s*\[.*?\]\s*\]", re.DOTALL),
    re.compile(r"\{\{.*?\}\}", re.DOTALL),
    re.compile(rf"\[\s*(?:{_TTS_TAG_NAMES})\s*:[^\]]*\]", re.IGNORECASE),
    re.compile(r"\[\[.*$", re.DOTALL),
)

# Only the well-formed shape is routine; the rest mean the model mis-rendered
# a tag, which is worth a log line because it is otherwise invisible -- it
# never reaches the transcript, it only ever gets *heard*.
_TTS_TAG_MALFORMED = _TTS_TAG_PATTERNS[1:]


def _strip_tag_like(text: str) -> str:
    """Drop meta tags and their content from text bound for the speaker."""
    cleaned = text
    for pattern in _TTS_TAG_PATTERNS:
        if pattern in _TTS_TAG_MALFORMED:
            found = pattern.search(cleaned)
            if found is not None:
                log.info(
                    "tts: dropped mis-rendered meta tag %r",
                    found.group(0)[:80],
                )
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def prepare_tts_text(text: str) -> str:
    """Clean text for TTS playback (audio path only; transcript is untouched)."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    # Remove fenced code blocks entirely
    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
    # Remove inline code
    cleaned = cleaned.replace("`", "")
    # Remove markdown headers (e.g. "## Title" -> "Title")
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.replace("#", "")
    # Remove URLs
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    # Remove bullet markers at line start
    cleaned = re.sub(r"^[\-\*]\s+", "", cleaned, flags=re.MULTILINE)
    # Strip bold / italic markdown
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*(.+?)\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"_(.+?)_", r"\1", cleaned)
    # Phase 3c: drop ``[[correct]]old[[/correct]]`` blocks entirely so
    # TTS only speaks the corrected text (the ``new`` half lives
    # *outside* the block). Done before the generic ``[[...]]`` strip
    # below so the inner ``old`` text doesn't slip through.
    cleaned = re.sub(
        r"\[\[correct\]\][\s\S]*?\[\[/correct\]\]",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Remove any remaining meta tag (reaction, spoken, detail, ...) *with its
    # content*. This is the last gate before audio, so it has to assume the
    # model mis-rendered the syntax: see ``_TTS_TAG_PATTERNS``.
    cleaned = _strip_tag_like(cleaned)
    # Remove stray brackets left in ordinary prose ("array[0]").
    cleaned = cleaned.replace("[", "").replace("]", "")
    # Replace very long numbers with a speakable placeholder
    cleaned = re.sub(r"\d{7,}", "a large number", cleaned)
    # Strip tildes -- Kokoro reads them literally
    cleaned = cleaned.replace("~", "")
    # Emoji + emoticons. The streaming voice path hands us raw model text, so
    # this cannot be left to ``sanitize_assistant_text`` -- that only cleans
    # the copy destined for the transcript, and by then the audio has played.
    cleaned = _EMOJI_RE.sub(" ", cleaned)
    cleaned = _EMOTICON_RE.sub(" ", cleaned)
    # The glued form ("hey:P"), which the shared pattern deliberately skips to
    # protect "3:30" and "https://". Safe here: URLs are already gone, and a
    # letter followed by ":P" / ":D" is never prose.
    cleaned = re.sub(r"(?<=[A-Za-z])[:;=][-o*']?[DPpOo3](?![\w])", " ", cleaned)
    # The heart that arrived with its "<" already missing (see
    # ``_SWALLOWED_HEART_RE``). Runs after the faces so an intact "<3" is
    # gone by now and only the orphaned digit reaches this line.
    cleaned = _SWALLOWED_HEART_RE.sub(" ", cleaned)
    # Ranges first, so "3-4 hours" keeps its sense instead of turning into two
    # unrelated numbers.
    cleaned = re.sub(r"(?<=\d)\s*[\u2010-\u2015\u2212-]\s*(?=\d)", " to ", cleaned)
    # Every other dash becomes a space. An em dash makes the model lurch into
    # a pause it never recovers the rhythm from, and a hyphenated compound
    # ("well-known") can come out as two clipped words; a plain space reads as
    # neither. This is also why a dash-bracketed filler ("I mean -- uhm --
    # yeah") is no longer stripped downstream: it becomes an unbracketed one,
    # which ``strip_speech_fillers`` deliberately leaves alone.
    cleaned = re.sub(r"[\u2010-\u2015\u2212-]+", " ", cleaned)
    # Symbols with no spoken form of their own. Deliberately excludes the ones
    # that carry meaning aloud (``% & $ / + =``), which the model does voice.
    cleaned = re.sub(r"[_|\\<>{}^]+", " ", cleaned)
    # Strip double quotes -- the TTS model occasionally vocalises a stray
    # or empty pair ('""') as a glitchy artifact. Apostrophes (single
    # quotes) are kept so contractions ("don't") survive.
    cleaned = cleaned.replace('"', "")
    # Speak filename / extension dots so the model doesn't read ".ext" as
    # a sentence terminator and insert a pause ("report.txt" -> "report
    # dot txt"). Only fires when a letter directly follows the dot, so
    # decimals (3.14) and version numbers (v2.0) are left for the model
    # to read normally.
    cleaned = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z])", " dot ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned


# K49: the non-lexical hesitation sounds, longest-first so "uhm" wins over
# "uh" and "mmhm" over "mm". Deliberately narrow -- these are the ones a
# grapheme-driven engine has to guess at, and Pocket-TTS has no phoneme
# control, so "uhm" can surface as "uh-em". Ordinary interjections ("wow",
# "oh", "huh", "yeah", "oof") are real words the model already says
# correctly, so they are never stripped and stay in the audio.
_TTS_FILLER_WORDS = (
    "uhm", "uhh", "uh",
    "ummm", "umm", "um",
    "mmhm", "mhm", "mmh", "mmm", "mm",
    "hmmm", "hmm", "hm",
    "erm", "er", "eh",
)

# A filler is only removed when it is its own clause: at the start of the
# text or after sentence/clause punctuation, AND followed by punctuation of
# its own. "it's, uhm, complicated" qualifies; "the uh oh moment" does not.
# Requiring both sides is what keeps this from mangling grammar -- a bare
# "it's uhm complicated" is left alone rather than guessed at.
_TTS_FILLER_RE = re.compile(
    r"(^|[.!?;:]\s+|,\s*|--\s*|\u2014\s*)"
    r"(?:" + "|".join(_TTS_FILLER_WORDS) + r")"
    r"\s*(?:\.{2,3}|[,.!?]|--|\u2014)\s*",
    re.IGNORECASE,
)


def strip_speech_fillers(text: str) -> str:
    """Drop non-lexical hesitation fillers from text bound for TTS.

    For ``agent.speech_texture_spoken = false``: the persona invites small
    disfluencies, which read well in the transcript but are synthesised
    letter-by-letter by an engine with no phoneme control. This removes only
    the unpronounceable ones from the spoken stream, leaving the transcript
    untouched (callers apply it on the TTS branch only).

    Never returns empty for non-empty input. A reply that is *entirely* a
    filler ("Mhm.") is a real acknowledgement rather than a stumble, so
    stripping it would silence a deliberate turn; those pass through.
    """
    original = str(text or "")
    if not original.strip():
        return original
    cleaned = original
    # Repeat to catch runs ("uhm, mm, yeah") -- one pass consumes the
    # separator the next match would need as its leading boundary. Bounded
    # so a pathological input can't spin.
    for _ in range(4):
        stripped = _TTS_FILLER_RE.sub(r"\1", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    # Removing a trailing filler can leave dangling clause punctuation
    # ("yeah, mm." -> "yeah,").
    cleaned = re.sub(r"[,;:]+\s*$", ".", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned.strip(".,;:!? "):
        return original
    return cleaned


def infer_tts_reaction(text: str) -> str:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return "neutral"

    if "[action]" in lowered:
        return "excited"
    if any(token in lowered for token in ("!", "wow", "amazing", "great", "awesome")):
        return "excited"
    if any(token in lowered for token in ("surprised", "unexpected", "didn't expect", "whoa")):
        return "surprised"
    if any(token in lowered for token in ("sorry", "unfortunately", "sad", "regret")):
        return "sad"
    if any(token in lowered for token in ("angry", "frustrated", "annoyed", "this is wrong")):
        return "angry"
    if any(token in lowered for token in ("calm", "let's slow", "take it step", "no rush")):
        return "calm"
    return "neutral"


def drain_tts_stream_chunks(buffer: str, *, flush: bool) -> tuple[list[str], str]:
    text = str(buffer or "")
    if not text:
        return [], ""

    chunks: list[str] = []
    start = 0
    for index, ch in enumerate(text):
        if ch == "\n":
            pass  # a newline is always a hard boundary
        elif ch in ".!?":
            nxt = text[index + 1] if index + 1 < len(text) else ""
            # A terminator glued to a word char on the right is *inside*
            # a token, not a sentence end: file.ext, 3.14, U.S.A,
            # Yahoo!Inc. An empty ``nxt`` means the terminator is the
            # last char streamed so far -- wait for the next delta to
            # reveal whether it's "done. " or "report.txt" (the flush
            # path emits any trailing remainder regardless).
            if nxt == "" or nxt.isalnum():
                continue
        else:
            continue

        candidate = text[start : index + 1].strip()
        if not candidate:
            start = index + 1
            continue

        if len(candidate) >= 24 or candidate.count(" ") >= 4 or ch == "\n":
            chunks.append(candidate)
            start = index + 1

    remainder = text[start:]
    if flush and remainder.strip():
        chunks.append(remainder.strip())
        remainder = ""

    return chunks, remainder
