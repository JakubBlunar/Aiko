"""History ages must never escape as reply prefixes, even one character at a time."""

from datetime import timedelta

import pytest

from app.core.infra import timephrase
from app.core.services.response_text_service import safe_visible_prefix, strip_all_meta_tags
from tests.test_turn_runner_mood_fallback import _build_runner


@pytest.mark.parametrize("minutes", [0, 2, 61, 180, 1500, 4500, 15000])
def test_real_history_age_formats_are_removed(minutes):
    now = timephrase.now()
    stamp = (now - timedelta(minutes=minutes)).isoformat()
    age = timephrase.age_prefix(stamp, now)
    raw = f"[{age}] Hello there."
    assert strip_all_meta_tags(raw) == "Hello there."


@pytest.mark.parametrize("raw", [
    "[now] Hello there.",
    "[NOW]\nHello there.",
    "  [ now ]\n\t Hello there.",
    "[[now]] Hello there.",
    "[now]] Hello there.",
    "[now] [neutral] Hello there.",
    "[[reaction:cheerful]]\n[now] Hello there.",
    "[just now] Hello there.",
    "  [just now]\n\t Hello there.",
    "[just now] [2 min ago] Hello there.",
    "[neutral]\nHello there.",
    "  [Neutral] Hello there.",
    "[[warm]] Hello there.",
    "[thoughtful+warm] Hello there.",
    "[just now] [neutral] Hello there.",
    "[[reaction:cheerful]]\n[just now] Hello there.",
    "[[reaction:cheerful]]\n[[prosody:soft]] [today 13:32 (2h 3m ago)] Hello there.",
])
def test_every_stream_split_keeps_ui_speech_and_storage_aligned(raw):
    for split in range(len(raw) + 1):
        runner = _build_runner(stream_tokens=[raw[:split], raw[split:]])
        visible, spoken = [], []
        result = runner.run(
            session_key="synthetic:age-prefix",
            user_text="hi",
            on_token=visible.append,
            on_tts_chunk=lambda text, mood, sink=spoken: sink.append(text),
        )
        assert result.text == "Hello there."
        assert "".join(visible).strip() == "Hello there."
        assert " ".join(spoken) == "Hello there."
        saved = [
            call.kwargs["content"] for call in runner._db.add_message.call_args_list
            if call.kwargs.get("role") == "assistant"
        ]
        assert saved == ["Hello there."]


@pytest.mark.parametrize("raw", [
    "[now] Hello there.",
    "[[now]] Hello there.",
    "[now] [neutral] Hello there.",
    "[now](https://example.com) is a link.",
    "[now] [reference] is a link.",
    "[now",
    "[just now] Hello there.",
    "[neutral]\nHello there.",
    "[neutral] [warm] Hello there.",
    "[neutral](https://example.com) is a link.",
    "[neutral] [reference] is a link.",
    "[[reaction:cheerful]]\n[just now] Hello there.",
    "[just now](https://example.com) is a link.",
    "[just now] [reference] is a link.",
    "[ordinary label] is prose.",
    "  Indented prose.",
    "[just now",  # A truncated literal is released on final flush.
])
def test_character_stream_is_monotonic_and_matches_final_text(raw):
    emitted = ""
    for end in range(1, len(raw) + 1):
        visible = safe_visible_prefix(raw[:end])
        assert visible.startswith(emitted)
        emitted = visible
    assert strip_all_meta_tags(raw).startswith(emitted)


@pytest.mark.parametrize("raw", [
    '"[now]" is metadata.',
    "`[now]` is metadata.",
    "```text\n[now] Hello\n```",
    "[now](https://example.com)",
    "[now][reference]",
    "[now] [reference]",
    "[now]: https://example.com",
    "The label [now] means current.",
    "Now I was reading.",
    "Hello.\n[now] is a history label.",
    '"[just now]" is metadata.',
    "`[just now]` is metadata.",
    "```text\n[just now] Hello\n```",
    "[just now](https://example.com)",
    "[just now][reference]",
    "[just now] [reference]",
    "[just now]: https://example.com",
    "The label [just now] means recent.",
    "[weather] Hello.",
    '"[neutral]" is a mood label.',
    "`[neutral]` is a mood label.",
    "```text\n[neutral] Hello\n```",
    "[neutral](https://example.com)",
    "[neutral][reference]",
    "[neutral] [reference]",
    "[neutral]: https://example.com",
    "The label [neutral] is a mood.",
    "[ordinary label] Hello.",
    "[12:34] Hello.",
    "Just now I was reading.",
    "Hello.\n[just now] is a history label.",
])
def test_literal_brackets_code_links_and_natural_time_survive(raw):
    assert strip_all_meta_tags(raw) == raw


@pytest.mark.parametrize("label", ["[just now]", "[now]", "[[now]]", "[ now ]"])
def test_whitespace_after_closed_label_waits_for_body(label):
    assert safe_visible_prefix(label) == ""
    assert safe_visible_prefix(label + "  \n ") == ""
    assert safe_visible_prefix(label + "  \n H") == "H"
    assert strip_all_meta_tags(label) == ""


def test_bare_mood_does_not_become_a_control_tag():
    from app.core.services.response_text_service import parse_reaction_at_start

    assert parse_reaction_at_start("[neutral] Hello.") == (None, "[neutral] Hello.")
    assert safe_visible_prefix("[neutral]") == ""
    assert safe_visible_prefix("[neutral] \n ") == ""
    assert safe_visible_prefix("[neutral] \n H") == "H"
    assert strip_all_meta_tags("[neutral]") == ""
