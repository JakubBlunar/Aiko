# K104 / K105: cause-specific feelings, quiet expression

Implemented slice, 24 October 2026. K104 now preserves independent causes;
K105 adds a pause-only cue and a decorative mood mist within the existing
voice and avatar limits. This is not a completed listening evaluation.

## K104: feelings keep their causes

The K57 episode cap, emotion vocabulary and decay rates are unchanged.
Episodes now have a bounded cause identity. Producers may supply a source
event ID; legacy rows use a deterministic source/cause fingerprint without
an offline migration. User reactions identify their message; kept-promise
warmth identifies the reply. Each new absence gets its own event identity.

An update affects only that emotion and cause. Repeated evidence does not
increase intensity, restart decay or apply another scalar affect impulse.
A stronger observation can update that cause without overwriting others.
An unrelated warm moment no longer cancels miffed or halves hurt.

Grievance acknowledgment needs both acknowledgment/clarification language
and lexical overlap with the cause in the same clause; negated apologies
are excluded. A generic apology does not clear every active
episode. Resolution targets an episode ID, emits the existing one-shot
thaw, and retains the last twelve resolved emotion/cause identities to
prevent replay. Explicit debug resolution can target an ID too. Reunion
language still resolves loneliness. This is conservative lexical matching,
not a claim of semantic understanding or a new inference model.

The existing T5 emotion block selects up to two different feelings; a pair
must contain a positive feeling. Negative feelings need cause relevance to
the current user text. Unrelated ones continue decaying without demanding
expression. A mixed cue is limited to 800 characters and asks for one
proportionate response without reciting labels, alternating faces or
reviving unrelated issues. The K60 mask retains its existing single-feeling
policy, and a thaw still takes precedence. This is register context, with
no new proactive offer or stance category.

The persona also permits excitement, uncertainty and care to coexist and
asks that clarification change the feeling it actually explains. These
remain natural-language reasoning over the conversation, not three new
persistent emotion producers. There are no new grievance triggers, decay
penalties or automatic semantic cause matches.

## K105: a supported beat and a visible atmosphere

`[[prosody:pause]]` is an optional 180 ms silent beat before one sentence,
limited to one explicit cue per reply on the streaming path.
A separate streaming speech copy retains this cue while the visible copy
continues stripping it, including across partial token boundaries. It passes through the main TurnRunner's speech cleanup into the shared
cadence dispatcher, then uses the existing queue silence path. It adds no
speed multiplier, gain change, filler, earcon or extra LLM request. It does
not rewrite punctuation when real silence is available. A blank cue emits
nothing, and the following sentence returns to its existing behavior.
Stored and displayed text use the existing metadata stripper.

The main streaming path previously stripped all prosody tags before cadence
could see them. Only the new pause cue is preserved at this boundary:
enabling previously discarded fast/slow/soft tags would change established
speech, contrary to this slice's pace constraint. Existing cadence speed
and engine pacing are unchanged. The other prosody values remain available
to existing callers that dispatch directly to cadence.

`MoodMist` replaces the avatar's fixed orange/blue/green status aura with
soft radial veils behind the canvas. A lingering mood contributes 40% of
the color mix during speech and the current recognized reaction 60%.
After speech, the mood alone remains. Unknown/neutral reactions keep the
background mood. This displays existing state, not a new emotion detector.

Gold denotes joy, peach warmth, lilac playfulness, teal curiosity, blue
reflection, rose tenderness, muted indigo concern and gray-lilac rest.
Opacity crossfades take 2.8 seconds; veils drift over 16 and 21 seconds.
The edges fade to transparency, including in the persona overlay. There
are no particles, flashes, rig writes or audio changes. Hidden windows
pause the animation; reduced-motion preferences disable movement and
transitions. Settings -> Avatar -> Mood mist stores a per-device toggle.

## Verification and remaining evaluation

Regression coverage includes distinct causes of the same feeling, unrelated
warmth/apologies, replay after decay/resolution, legacy storage, bounded
mixed rendering, queue-level pause ordering, unchanged speech rate/gain,
empty cues, clean transcript text and stable color weight/brightness.

The isolated browser preview uses the existing Alexia assets and real
Live2DAvatar/MoodMist components without a backend or conversation data.
It verifies visual containment and color changes. It does not exercise the
packaged Tauri overlay, real synthesized listening, barge-in or a two-client
delivery comparison.

K105 remains partial. There is no new laugh synthesis, facial parameter,
word alignment or semantic expression director. The mist follows the
existing mood and server speech-state signals; it does not certify that a
particular word or sentence has been heard. Blinded listening, latency and
playback-identity evaluations from the proposal remain open.
