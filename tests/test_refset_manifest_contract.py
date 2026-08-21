"""The studio writes a reference layout that the app reads back.

Two programs and one on-disk shape. ``tools/tts_lab/refset.py`` builds a
reference out of found recordings; ``ChatterboxTtsService`` picks up her
brightness, tempo and sampling-knob settings from the result. Nothing in
either file
imports the other, and both failure modes are silent -- the app logs one
line at INFO and speaks perfectly well with tempo matching off -- so a
rename of ``parts/`` or of the ``phrase`` key would cost a feature
without costing a test. Hence this file: it is the only place the
contract is written down as an assertion.

The lab is otherwise untested on purpose, being a prototype sandbox that
``app/`` never imports. This is the one seam where that stops being true.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from tools.tts_lab import refset
from tools.tts_lab.adapters import read_wav, write_wav

RATE = 24000


def _tone(seconds: float, *, amp: float = 0.5, freq: float = 220.0):
    """Continuous audio of a known length.

    A tone rather than noise because ``speech_seconds`` gates on energy:
    an unbroken tone is voiced for its whole duration, which makes the
    measured syllable rate exactly ``syllables / seconds`` and the
    assertions below arithmetic rather than approximate.
    """
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


#: Read through one-second clips, so the delivered rate is this many
#: syllables per second. Counted by the app's own heuristic rather than
#: by hand: it is a vowel-group estimate and disagrees with a careful
#: reader on words like "scramble", which is a property of the estimator
#: and not something a contract test should be asserting about.
PHRASE = "the quiet morning arrives again"


class ManifestContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clips = self.root / "clips"
        self.clips.mkdir()
        # Deliberately different amplitudes: the level relationship
        # between parts is asserted below.
        for index, amp in enumerate((0.2, 0.5, 0.8, 0.35), start=1):
            write_wav(self.clips / f"c{index}.wav", _tone(1.0, amp=amp), RATE)
        self._saved_root = refset.CLIP_ROOT
        refset.CLIP_ROOT = self.root

    def tearDown(self) -> None:
        refset.CLIP_ROOT = self._saved_root
        self._tmp.cleanup()

    def _build(self, *, phrases: bool = True) -> tuple[Path, dict]:
        parts = [
            refset.Part(
                rel=f"clips/c{i}.wav", phrase=PHRASE if phrases else ""
            )
            for i in range(1, 5)
        ]
        out = self.root / "built"
        return out, refset.build(parts, out)

    # ── the layout ──

    def test_the_layout_is_the_one_the_app_looks_for(self) -> None:
        out, manifest = self._build()
        self.assertTrue((out / "reference.wav").is_file())
        # Beside the reference, not above it: the app resolves the
        # manifest as ``reference.parent / "manifest.json"``.
        self.assertTrue((out / "manifest.json").is_file())
        self.assertEqual(
            sorted(p.name for p in (out / "parts").glob("*.wav")),
            ["part01.wav", "part02.wav", "part03.wav", "part04.wav"],
        )
        for part in manifest["parts"]:
            self.assertTrue((out / "parts" / part["file"]).is_file())
            self.assertIn("phrase", part)

    def test_parts_keep_their_relative_level(self) -> None:
        """The one thing a per-clip normalise would quietly destroy.

        Range between takes is the speaker. Normalising each part to a
        fixed peak is the reflex, reads as tidier, and would hand the
        clone a reference in which a whisper and a shout are the same
        size.
        """
        out, manifest = self._build()
        peaks = [
            float(np.abs(read_wav(out / "parts" / p["file"])[0]).max())
            for p in manifest["parts"]
        ]
        self.assertLess(peaks[0], peaks[1])
        self.assertLess(peaks[1], peaks[2])
        self.assertGreater(peaks[2], peaks[3])

    def test_a_rebuild_does_not_leave_the_old_parts_behind(self) -> None:
        out, _ = self._build()
        refset.build([refset.Part(rel="clips/c1.wav")], out)
        self.assertEqual(
            [p.name for p in sorted((out / "parts").glob("*.wav"))],
            ["part01.wav"],
        )

    def test_a_clip_outside_the_root_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            refset.resolve_clip("../../etc/passwd")

    # ── what the app reads back ──

    def test_the_app_takes_a_tempo_target_from_the_manifest(self) -> None:
        from app.tts.chatterbox_service import ChatterboxTtsService

        out, manifest = self._build()
        service = ChatterboxTtsService.__new__(ChatterboxTtsService)
        # Four attributes rather than a constructed service: building one
        # spawns a sidecar and loads a model, and the target adoption
        # touches nothing else.
        service._engine_key = "test"
        service._rate_matching = True
        service._rate_limit = 0.15
        service._rate_target_syl_s = None
        service._adopt_rate_target(out / str(manifest["reference"]))
        self.assertIsNotNone(service._rate_target_syl_s)
        # The phrase, read through a one-second unbroken tone.
        from app.audio.speech_rate import syllables

        self.assertAlmostEqual(
            service._rate_target_syl_s, syllables(PHRASE), delta=1.0
        )

    def test_parts_without_phrases_give_the_app_no_tempo_target(self) -> None:
        """A found voice pack has no transcripts, and that is the safe case.

        Silence here is load-bearing: guessing a tempo from clips nobody
        labelled would aim her pacing at a number derived from nothing.
        """
        from app.tts.chatterbox_service import ChatterboxTtsService

        out, manifest = self._build(phrases=False)
        service = ChatterboxTtsService.__new__(ChatterboxTtsService)
        service._engine_key = "test"
        service._rate_matching = True
        service._rate_limit = 0.15
        service._rate_target_syl_s = None
        service._adopt_rate_target(out / str(manifest["reference"]))
        self.assertIsNone(service._rate_target_syl_s)

    def test_a_declared_target_is_honoured_without_any_transcripts(self) -> None:
        """The escape hatch for source audio in another language.

        Her recovered pack is Japanese game clips. They are unarguably
        her voice and there is no honest English transcript to measure a
        rate from, so the measured route gives no target and the clone
        runs slow with nothing able to correct it. Declaring the target
        says "this is her, hold her to her own pace".
        """
        from app.audio.speech_rate import DEFAULT_TARGET_SYL_S
        from app.tts.chatterbox_service import ChatterboxTtsService

        parts = [refset.Part(rel=f"clips/c{i}.wav") for i in range(1, 5)]
        out = self.root / "declared"
        manifest = refset.build(
            parts, out, target_syl_s=DEFAULT_TARGET_SYL_S
        )
        self.assertEqual(manifest["target_syl_s"], DEFAULT_TARGET_SYL_S)
        self.assertTrue(all(not p["phrase"] for p in manifest["parts"]))

        service = ChatterboxTtsService.__new__(ChatterboxTtsService)
        service._engine_key = "test"
        service._rate_matching = True
        service._rate_limit = 0.15
        service._rate_target_syl_s = None
        service._adopt_rate_target(out / str(manifest["reference"]))
        self.assertEqual(service._rate_target_syl_s, DEFAULT_TARGET_SYL_S)

    def test_a_declaration_wins_over_measuring_the_parts(self) -> None:
        """Deliberate, and the reason the declaration exists at all.

        A declared target is an instruction; measured parts are an
        inference. Where both are present the instruction is the one
        somebody chose.
        """
        from app.tts.chatterbox_service import ChatterboxTtsService

        parts = [
            refset.Part(rel=f"clips/c{i}.wav", phrase=PHRASE)
            for i in range(1, 5)
        ]
        out = self.root / "both"
        manifest = refset.build(parts, out, target_syl_s=4.0)
        service = ChatterboxTtsService.__new__(ChatterboxTtsService)
        service._engine_key = "test"
        service._rate_matching = True
        service._rate_limit = 0.15
        service._rate_target_syl_s = None
        service._adopt_rate_target(out / str(manifest["reference"]))
        self.assertEqual(service._rate_target_syl_s, 4.0)

    def test_knobs_tuned_in_the_studio_reach_the_app(self) -> None:
        """The reason the panel is worth having.

        The app sent no generation kwargs at all before this, so every
        voice spoke on its engine's shipped defaults and a value found in
        an audition could not be carried anywhere.
        """
        from tools.tts_lab.serve import _record_knobs

        parts = [refset.Part(rel=f"clips/c{i}.wav") for i in range(1, 4)]
        out = self.root / "tuned"
        manifest = refset.build(parts, out)
        tuned = _record_knobs(
            out / "manifest.json",
            "chatterbox-nano",
            {"temperature": 0.5, "min_p": 0.05, "voice": "ignored"},
        )
        self.assertEqual(tuned, {"temperature": 0.5, "min_p": 0.05})

        service = self._service("chatterbox-nano")
        service._adopt_generate_kwargs(out / str(manifest["reference"]))
        self.assertEqual(
            service._generate_kwargs, {"temperature": 0.5, "min_p": 0.05}
        )

    def test_another_engine_ignores_the_tuning(self) -> None:
        """Absolute values chosen against defaults that are not shared.

        Nano ships ``min_p=0.0`` where the full model ships ``0.05``, and
        ``exaggeration``/``cfg_weight`` are ``0.0`` against ``0.5``. So
        replaying one engine's numbers on the other is not conservative,
        it quietly means something else.
        """
        from tools.tts_lab.serve import _record_knobs

        parts = [refset.Part(rel=f"clips/c{i}.wav") for i in range(1, 4)]
        out = self.root / "crossengine"
        manifest = refset.build(parts, out)
        _record_knobs(out / "manifest.json", "chatterbox-nano", {"min_p": 0.05})

        service = self._service("chatterbox-full")
        service._adopt_generate_kwargs(out / str(manifest["reference"]))
        self.assertEqual(service._generate_kwargs, {})

    def test_each_engine_keeps_its_own_tuning_of_one_reference(self) -> None:
        """One reference, several engines, one sitting.

        The point of having them side by side. A single block per voice
        would have let the second save quietly discard the first
        engine's afternoon.
        """
        from tools.tts_lab.serve import _record_knobs

        parts = [refset.Part(rel=f"clips/c{i}.wav") for i in range(1, 4)]
        out = self.root / "twoengines"
        manifest = refset.build(parts, out)
        path = out / "manifest.json"
        _record_knobs(path, "chatterbox-nano", {"min_p": 0.05})
        _record_knobs(path, "chatterbox-turbo", {"temperature": 0.6})

        reference = out / str(manifest["reference"])
        nano = self._service("chatterbox-nano")
        nano._adopt_generate_kwargs(reference)
        turbo = self._service("chatterbox-turbo")
        turbo._adopt_generate_kwargs(reference)
        self.assertEqual(nano._generate_kwargs, {"min_p": 0.05})
        self.assertEqual(turbo._generate_kwargs, {"temperature": 0.6})

    def test_clearing_one_engine_leaves_the_others_alone(self) -> None:
        from tools.tts_lab.serve import _record_knobs

        parts = [refset.Part(rel=f"clips/c{i}.wav") for i in range(1, 4)]
        out = self.root / "clearone"
        refset.build(parts, out)
        path = out / "manifest.json"
        _record_knobs(path, "chatterbox-nano", {"min_p": 0.05})
        _record_knobs(path, "chatterbox-turbo", {"temperature": 0.6})
        _record_knobs(path, "chatterbox-nano", {})

        block = json.loads(path.read_text(encoding="utf-8"))["generate"]
        self.assertEqual(block, {"chatterbox-turbo": {"temperature": 0.6}})

    def test_a_setting_outranks_the_voice_s_own_tuning(self) -> None:
        """The lever for a bare wav, and for trying a value in the app."""
        from tools.tts_lab.serve import _record_knobs

        parts = [refset.Part(rel=f"clips/c{i}.wav") for i in range(1, 4)]
        out = self.root / "override"
        manifest = refset.build(parts, out)
        _record_knobs(
            out / "manifest.json", "chatterbox-nano", {"temperature": 0.5}
        )

        service = self._service("chatterbox-nano")
        service._settings_kwargs = {"temperature": 0.7}
        service._adopt_generate_kwargs(out / str(manifest["reference"]))
        self.assertEqual(service._generate_kwargs, {"temperature": 0.7})

    def test_clearing_the_last_engine_removes_the_block_entirely(self) -> None:
        from tools.tts_lab.serve import _record_knobs

        parts = [refset.Part(rel=f"clips/c{i}.wav") for i in range(1, 4)]
        out = self.root / "cleared"
        refset.build(parts, out)
        path = out / "manifest.json"
        _record_knobs(path, "chatterbox-nano", {"temperature": 0.5})
        _record_knobs(path, "chatterbox-nano", {})
        body = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("generate", body)

    def _service(self, engine_key: str):
        from app.tts.chatterbox_service import ChatterboxTtsService

        service = ChatterboxTtsService.__new__(ChatterboxTtsService)
        service._engine_key = engine_key
        service._settings_kwargs = {}
        service._generate_kwargs = {}
        return service

    def test_the_app_takes_a_brightness_target_from_the_wav(self) -> None:
        from app.tts.chatterbox_service import ChatterboxTtsService

        out, manifest = self._build()
        service = ChatterboxTtsService.__new__(ChatterboxTtsService)
        service._engine_key = "test"
        service._tilt_matching = True
        service._tilt_limit_db = 4.0
        service._tilt_target_db = None
        service._adopt_tilt_target(out / str(manifest["reference"]))
        self.assertIsNotNone(service._tilt_target_db)

    def test_the_studio_predicts_what_the_app_will_adopt(self) -> None:
        """The studio's report and the app's behaviour are one number.

        The report exists so a reference can be judged before it is
        saved; a report that disagrees with the engine would be worse
        than none.
        """
        from app.tts.chatterbox_service import ChatterboxTtsService

        out, manifest = self._build()
        targets = refset.app_targets(out, manifest)
        self.assertEqual(targets["rate_parts"], 4)
        self.assertIsNotNone(targets["tilt_db"])

        service = ChatterboxTtsService.__new__(ChatterboxTtsService)
        service._engine_key = "test"
        service._rate_matching = True
        service._rate_limit = 0.15
        service._rate_target_syl_s = None
        service._adopt_rate_target(out / str(manifest["reference"]))
        self.assertAlmostEqual(
            targets["rate_syl_s"], service._rate_target_syl_s, places=1
        )

    def test_a_tempo_far_from_her_pace_is_called_out(self) -> None:
        """The trap this warning exists for.

        A found pack is one-word interjections. Transcribing them
        helpfully yields a target near 2.5 syllables per second against
        her established 6.55, which would stretch every sentence she
        speaks to the correction limit -- permanently, on evidence from
        clips that are single words.
        """
        from app.audio.speech_rate import DEFAULT_TARGET_SYL_S

        slow = self.root / "slow"
        slow.mkdir()
        # Same seven syllables over three seconds: 2.3 syllables/second.
        for index in range(1, 5):
            write_wav(slow / f"s{index}.wav", _tone(3.0), RATE)
        parts = [
            refset.Part(rel=f"slow/s{i}.wav", phrase=PHRASE)
            for i in range(1, 5)
        ]
        out = self.root / "built_slow"
        manifest = refset.build(parts, out)
        targets = refset.app_targets(out, manifest)
        self.assertLess(targets["rate_syl_s"], DEFAULT_TARGET_SYL_S * 0.85)
        self.assertIn("off her established", targets["rate_warning"])

    def test_an_unmeasurable_transcript_says_which_and_why(self) -> None:
        out = self.root / "built_short"
        manifest = refset.build(
            [refset.Part(rel="clips/c1.wav", phrase="hi")], out
        )
        targets = refset.app_targets(out, manifest)
        self.assertEqual(targets["rate_parts"], 0)
        self.assertEqual(len(targets["rate_skipped"]), 1)
        self.assertIn("syllables", targets["rate_skipped"][0]["why"])


class ConditioningWindowTests(unittest.TestCase):
    """Where the engine's two cuts land, which is what ordering decides."""

    def _manifest(self, durations: list[float]) -> dict:
        parts = []
        at = 0.0
        for index, seconds in enumerate(durations, start=1):
            parts.append(
                {
                    "file": f"part{index:02d}.wav",
                    "source": f"c{index}",
                    "duration_s": seconds,
                    "starts_at_s": at,
                }
            )
            at += seconds + 0.22
        return {"parts": parts, "reference_duration_s": at - 0.22}

    def test_clips_past_the_tokenizer_window_are_named_as_discarded(self) -> None:
        got = refset.windows(self._manifest([6.0, 6.0, 6.0, 6.0]))
        # 0-6, 6.22-12.22, 12.44-18.44, 18.66-24.66.
        self.assertEqual(got["in_decoder"], ["c1", "c2"])
        self.assertEqual(got["discarded"], ["c4"])
        self.assertGreater(got["over_budget_s"], 9.0)

    def test_a_clip_cut_in_half_by_the_decoder_is_flagged(self) -> None:
        got = refset.windows(self._manifest([9.0, 4.0]))
        # The second starts at 9.22 and runs to 13.22, so the decoder
        # hears 0.78s of it and nothing warns you in a player.
        self.assertEqual(got["straddling"], ["c2"])

    def test_a_short_reference_wastes_nothing(self) -> None:
        got = refset.windows(self._manifest([2.0, 2.0]))
        self.assertEqual(got["discarded"], [])
        self.assertEqual(got["straddling"], [])
        self.assertEqual(got["over_budget_s"], 0.0)


class SuggestionTests(unittest.TestCase):
    def _clip(self, rel: str, *, seconds: float, khz: float) -> refset.Clip:
        return refset.Clip(
            rel=rel,
            duration_s=seconds,
            sample_rate=44100,
            peak=0.7,
            rms=0.1,
            silence_share=0.1,
            bandwidth_hz=khz * 1000.0,
        )

    def test_the_gap_counts_against_the_budget(self) -> None:
        """Seven gaps are a second and a half of the ten being filled.

        Ignoring them looks like rounding and is not: a selection
        totalling ten seconds of speech is nearly twelve of reference,
        and the tail falls past the cut it was chosen to fill.
        """
        clips = [
            self._clip(f"c{i}.wav", seconds=1.2, khz=15.0) for i in range(10)
        ]
        chosen = refset.rank(clips, seconds=10.0)
        total = 1.2 * len(chosen) + 0.22 * (len(chosen) - 1)
        self.assertLessEqual(total, 10.0)
        self.assertGreater(total, 8.0)

    def test_brighter_clips_come_first(self) -> None:
        clips = [
            self._clip("dull.wav", seconds=2.0, khz=6.0),
            self._clip("bright.wav", seconds=2.0, khz=15.0),
        ]
        self.assertEqual(refset.rank(clips, seconds=3.0), ["bright.wav"])

    def test_connected_speech_beats_a_brighter_single_word(self) -> None:
        """The mistake the first version of this made, and it was audible.

        Chatterbox clones pacing along with timbre. Ten seconds of the
        brightest available clips came out of one-word game lines at a
        0.92 s median and delivered 5.50 syllables per second against
        7.36 from her sentence-length reference -- a 34% drawl, from a
        selection that scored perfectly on every quality number.
        """
        clips = [
            self._clip("word.wav", seconds=0.9, khz=18.0),
            self._clip("sentence.wav", seconds=4.0, khz=12.0),
        ]
        self.assertEqual(refset.rank(clips, seconds=5.0)[0], "sentence.wav")

    def test_short_clips_still_top_up_a_budget_nothing_else_can_fill(self) -> None:
        clips = [
            self._clip("word.wav", seconds=0.9, khz=18.0),
            self._clip("sentence.wav", seconds=2.0, khz=12.0),
        ]
        self.assertEqual(
            refset.rank(clips, seconds=10.0),
            ["sentence.wav", "word.wav"],
        )

    def test_a_flawed_clip_is_never_suggested(self) -> None:
        bad = self._clip("clipped.wav", seconds=2.0, khz=18.0)
        bad.warnings = ("clipped",)
        good = self._clip("fine.wav", seconds=2.0, khz=9.0)
        self.assertEqual(refset.rank([bad, good]), ["fine.wav"])


class SaveMergeTests(unittest.TestCase):
    """Saving the same voice twice, once per engine.

    A unit test of the merge passed while this was broken, because the
    merge was never the part that was wrong: ``/api/save`` copies the
    build's manifest over the destination, and the build has no
    ``generate`` block, so the first engine's numbers were wiped before
    the merge ever ran. Only an end-to-end save shows it.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.voices = self.root / "voices"
        self.voices.mkdir()
        self._clip_root = refset.CLIP_ROOT
        refset.CLIP_ROOT = self.root / "clips"
        (self.root / "clips").mkdir()
        write_wav(self.root / "clips" / "c1.wav", _tone(2.0), RATE)

    def tearDown(self) -> None:
        refset.CLIP_ROOT = self._clip_root
        self._tmp.cleanup()

    def _save(self, engine: str, knobs: dict) -> Path:
        """The save path's manifest handling, without the HTTP layer."""
        import shutil

        from tools.tts_lab.serve import _record_knobs, _tuned_knobs

        source = self.root / "build"
        if not source.exists():
            refset.build([refset.Part(rel="c1.wav")], source)
        dest = self.voices / "voice"
        dest.mkdir(exist_ok=True)
        manifest = dest / "manifest.json"
        carried = _tuned_knobs(manifest)
        shutil.copytree(source, dest, dirs_exist_ok=True)
        for key, values in carried.items():
            _record_knobs(manifest, key, values)
        _record_knobs(manifest, engine, knobs)
        return manifest

    def test_a_second_engine_s_save_keeps_the_first_s_tuning(self) -> None:
        self._save("chatterbox-nano", {"min_p": 0.05})
        manifest = self._save("chatterbox-turbo", {"temperature": 0.6})
        block = json.loads(manifest.read_text(encoding="utf-8"))["generate"]
        self.assertEqual(
            block,
            {
                "chatterbox-nano": {"min_p": 0.05},
                "chatterbox-turbo": {"temperature": 0.6},
            },
        )

    def test_resaving_one_engine_replaces_only_its_own_numbers(self) -> None:
        self._save("chatterbox-nano", {"min_p": 0.05})
        self._save("chatterbox-turbo", {"temperature": 0.6})
        manifest = self._save("chatterbox-nano", {"min_p": 0.1})
        block = json.loads(manifest.read_text(encoding="utf-8"))["generate"]
        self.assertEqual(block["chatterbox-nano"], {"min_p": 0.1})
        self.assertEqual(block["chatterbox-turbo"], {"temperature": 0.6})


class ShapeTests(unittest.TestCase):
    """Connected speech or a list of words, which a player cannot tell."""

    def _manifest(self, lengths: list[float], *, gap_ms: int = 220) -> dict:
        parts = [
            {"file": f"part{i:02d}.wav", "duration_s": s}
            for i, s in enumerate(lengths, start=1)
        ]
        total = sum(lengths) + gap_ms / 1000.0 * (len(lengths) - 1)
        return {
            "parts": parts,
            "gap_ms": gap_ms,
            "reference_duration_s": total,
        }

    def test_a_reference_of_single_words_is_called_out(self) -> None:
        got = refset.shape(self._manifest([0.9] * 10))
        self.assertFalse(got["connected"])
        self.assertIn("isolated words", got["warning"])

    def test_sentence_length_parts_pass(self) -> None:
        got = refset.shape(self._manifest([3.0, 3.5, 3.0]))
        self.assertTrue(got["connected"])
        self.assertEqual(got["warning"], "")
        self.assertAlmostEqual(got["median_part_s"], 3.0, places=2)

    def test_too_much_of_the_reference_being_gaps_is_called_out(self) -> None:
        """Connected parts, but chopped fine enough to teach pausing."""
        got = refset.shape(self._manifest([1.5] * 8, gap_ms=400))
        self.assertTrue(got["connected"])
        self.assertIn("silence", got["warning"])

    def test_an_empty_manifest_reports_nothing_rather_than_failing(self) -> None:
        self.assertEqual(refset.shape({"parts": []}), {})


class VoiceListingTests(unittest.TestCase):
    def test_reference_fragments_are_not_offered_as_voices(self) -> None:
        """The 279-entry dropdown, one directory later.

        Every reference set carries a ``parts/`` folder of a dozen
        fragments. The old rule excluded the literal ``reference/parts/``,
        which was every set that existed when it was written.
        """
        from tools.tts_lab.serve import _is_scratch

        self.assertFalse(_is_scratch("aiko-probe/reference.wav"))
        self.assertTrue(_is_scratch("aiko-probe/parts/part01.wav"))
        self.assertTrue(_is_scratch("reference/parts/part01.wav"))
        self.assertTrue(_is_scratch("sounds/aiko-original/GO-go2.mp3"))
        self.assertFalse(_is_scratch("reference/aiko_reference.wav"))


if __name__ == "__main__":
    unittest.main()
