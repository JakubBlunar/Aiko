"""A live voice turn over the production WebSocket with a scripted chat model."""
import json
import os
import re
import struct
import time
import wave
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy.signal import resample_poly

from app.audio.client_mic_source import ClientMicSource
from app.core.brain import BrainLoop, KIND_USER_MESSAGE
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import load_settings
from app.core.session.live_session import LiveSession
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.core.voice.tts_queue import TtsQueue
from app.llm.ollama_client import OllamaUsage
from app.stt.realtime_stt_service import RealtimeSttService
from app.web import audio_frames as frames
from app.web.server import create_web_app
from tests.test_voice_merge import _make_controller


QUESTIONS = ("The quick brown fox jumps over the lazy dog.", "What color is the fox?")
REPLIES = ("The dog is sleeping.", "The fox is brown.")
FIXTURES = Path(__file__).parent / "fixtures" / "voice"


def test_transcript_finishing_after_voice_stop_stays_off_screen():
    events = Mock()
    live = LiveSession(Mock(), events)
    live._active = True
    live.stop()
    live._on_transcript("Late final transcript")
    events.assert_not_called()


class ScriptedSpeech:
    is_available = True
    is_loaded = True

    def transcribe(self, path):
        self.captures.append(Path(path))
        with wave.open(str(path), "rb") as clip:
            assert (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) == (16000, 1, 2)
            assert np.frombuffer(clip.readframes(clip.getnframes()), "<i2").max() > 1000
        return next(self.transcripts)

    def __init__(self, session):
        self.session = session
        self.captures = []
        self.transcripts = iter(QUESTIONS)
        self.spoken = []

    def speak_async(self, text, *, on_done, **kwargs):
        self.spoken.append(text.strip())
        self.session._emit_audio_frame("tts", 24000, 1, b"\x00\x10" * 2400)
        self.session._emit_audio_frame_end("tts")
        on_done()

    def stop(self):
        self.session._emit_audio_frame_cancel("tts")


async def _receive(ws, deadline):
    # TestClient's public receive() can otherwise hang forever on a lost clip.
    with anyio.fail_after(max(0, deadline - time.monotonic())):
        return await ws._send_rx.receive()


def _exchange(ws, samples, *, timeout, frame_interval, hold_tail=False,
              release_previous=False, expect_cancel=False):
    events = []
    # Feed the same 50 ms frames as the browser worklet, including trailing silence.
    for offset in range(0, len(samples), 2400):
        if expect_cancel and offset == 48000 * 5 // 4:
            # The recording still contains speech. Aiko should yield before
            # receiving the rest of the question or its endpoint silence.
            deadline = time.monotonic() + timeout
            while True:
                packet = ws.portal.call(_receive, ws, deadline)
                if "bytes" in packet:
                    assert packet["bytes"] == bytes(
                        [frames.FRAME_AUDIO_CANCEL, frames.FRAME_TTS_PCM],
                    )
                    break
                event = json.loads(packet["text"])
                assert event["type"] not in {"stt_final", "token", "turn_done", "error"}, event
                events.append(event)
        ws.send_bytes(bytes([frames.FRAME_MIC_PCM]) + samples[offset:offset + 2400].tobytes())
        time.sleep(frame_interval)

    deadline = time.monotonic() + timeout
    if release_previous:
        # With barge-in disabled the complete second phrase stays queued
        # while the client is still playing the first reply.
        ws.send_json({"type": "ping"})
        while True:
            packet = ws.portal.call(_receive, ws, deadline)
            assert "bytes" not in packet, "audio arrived before playback drained"
            event = json.loads(packet["text"])
            assert event["type"] not in {"stt_final", "token", "turn_done", "error"}, event
            if event["type"] == "pong":
                break
        ws.send_json({"type": "playback_drained"})
    audio = bytearray()
    frame_types = []
    heard_end = False
    turn_done = False
    listening = False
    rate = 0
    while not (turn_done and heard_end and (listening or hold_tail)):
        packet = ws.portal.call(_receive, ws, deadline)
        if "bytes" in packet:
            kind, payload = packet["bytes"][0], packet["bytes"][1:]
            if expect_cancel and not frame_types and kind == frames.FRAME_AUDIO_END:
                # Pocket-TTS follows a cancel with an end for that same clip.
                continue
            frame_types.append(kind)
            if kind == frames.FRAME_AUDIO_START:
                stream, rate, channels = struct.unpack(">BIB", payload[:6])
                assert stream == frames.FRAME_TTS_PCM and channels == 1
            elif kind == frames.FRAME_TTS_PCM:
                audio.extend(payload)
            elif kind == frames.FRAME_AUDIO_END:
                heard_end = True
                if not hold_tail:
                    ws.send_json({"type": "playback_drained"})
        else:
            event = json.loads(packet["text"])
            assert event["type"] != "error", event
            events.append(event)
            if event["type"] == "turn_done":
                turn_done = True
            if turn_done and event == {"type": "voice_state", "state": "listening"}:
                listening = True
    assert frame_types[0] == frames.FRAME_AUDIO_START
    assert frame_types[-1] == frames.FRAME_AUDIO_END
    assert all(kind == frames.FRAME_TTS_PCM for kind in frame_types[1:-1])
    assert np.max(np.abs(np.frombuffer(audio, "<i2").astype(float))) > 100
    return events, audio, rate


@pytest.mark.parametrize(("real_speech", "interrupt"), [
    pytest.param(False, None, id="scripted"),
    pytest.param(False, True, id="scripted-interrupt"),
    pytest.param(False, False, id="scripted-wait"),
    pytest.param(True, None, id="real-speech"),
    pytest.param(True, True, id="real-speech-interrupt"),
])
def test_voice_roundtrip(tmp_path, real_speech, interrupt, monkeypatch, record_property):
    if real_speech and os.getenv("AIKO_TEST_REAL_SPEECH") != "1":
        pytest.skip("opt-in local speech models")
    settings = load_settings(None)
    db = ChatDatabase(tmp_path / "voice.db")
    session = _make_controller(db)
    session._settings = settings
    session._state = SimpleNamespace(mic_enabled=True)
    session._message_listeners = []
    session._tts_state_listeners = []
    session._microphone = ClientMicSource(settings.audio)
    session._vad_level_threshold = settings.audio.vad_level_threshold
    session._vad_silence_seconds = settings.audio.vad_silence_seconds
    settings.tts.enabled = True
    settings.stt.enabled = True
    if interrupt is not None:
        settings.audio.barge_in_enabled = interrupt
    # Unrelated avatar/model discovery is normally initialized during app boot.
    session.avatar_payload = lambda: {}
    session._avatar_root = tmp_path / "avatar"
    monkeypatch.setattr("app.core.tasks.attachments.ensure_attachments_dir", lambda: tmp_path)
    session._effective_chat_model = "scripted"
    session._missing_chat_model = ""
    settings.endpointing.use_partial_transcript = real_speech

    model = Mock(last_usage=OllamaUsage())
    model.chat_stream.side_effect = [[reply[:8], reply[8:]] for reply in REPLIES]
    persona = tmp_path / "persona.txt"
    persona.write_text("You are Aiko.", encoding="utf-8")
    session._prompt_assembler = PromptAssembler(db, persona_path=persona)
    session._turn_runner = TurnRunner(
        model, db, session._prompt_assembler, model="scripted",
        context_window=8192, max_tokens=128, temperature=0, filler_enabled=False,
        delivery_provider=session.delivery_ledger,
    )
    loop = BrainLoop()
    loop.register_handler(KIND_USER_MESSAGE, session._on_user_message_event)
    session._brain_loop = loop
    session._task_orchestration_enabled = True

    with ExitStack() as stack:
        stack.callback(db._get_conn().close)
        if real_speech:
            monkeypatch.setenv("HF_HUB_OFFLINE", "1")
            import torch
            from app.tts.pocket_tts_service import PocketTtsService

            engine = PocketTtsService(settings.tts)
            assert engine.warmup_sync(), engine.get_status()
            engine.set_pcm_listener(
                lambda rate, ch, pcm: session._emit_audio_frame("tts", rate, ch, pcm),
                end_listener=lambda: session._emit_audio_frame_end("tts"),
                cancel_listener=lambda: session._emit_audio_frame_cancel("tts"),
            )
            session._realtime_stt = RealtimeSttService(settings.stt, settings.audio)
            stack.callback(session._realtime_stt.shutdown)
            assert session._realtime_stt.prewarm(), session._realtime_stt._last_error
            # Pocket samples the pronunciation of each reply on CPU. Exercise
            # one repeatable rendition and restore the caller's RNG on exit.
            stack.enter_context(torch.random.fork_rng(devices=[]))
            torch.random.default_generator.manual_seed(42)
        else:
            engine = ScriptedSpeech(session)
            session._realtime_stt = engine
        session._tts = TtsQueue(engine, state_listener=session._on_tts_state)
        stack.callback(session._tts.stop)
        loop.start()
        stack.callback(loop.stop)
        exchanges = []
        with TestClient(create_web_app(session)) as client, client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "voice_start"})
            ws.send_bytes(bytes([frames.FRAME_MIC_START]) + struct.pack(">IBB", 48000, 1, 7))
            # Fixed Microsoft Zira synthetic speech, independent of the reply synthesizer.
            for index, filename in enumerate(("fox-and-dog.wav", "fox-follow-up.wav")):
                with wave.open(str(FIXTURES / filename), "rb") as clip:
                    assert (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) == (
                        16000, 1, 2,
                    )
                    samples = np.frombuffer(clip.readframes(clip.getnframes()), "<i2")
                pcm = resample_poly(samples.astype(float), 3, 1).clip(-32768, 32767).astype("<i2")
                exchanges.append(_exchange(
                    ws, np.pad(pcm, (0, 48000 * 3)), timeout=40 if real_speech else 8,
                    frame_interval=0.05 if real_speech else 0.006,
                    hold_tail=index == 0 and interrupt is not None,
                    release_previous=index == 1 and interrupt is False,
                    expect_cancel=index == 1 and interrupt is True,
                ))
            ws.send_json({"type": "voice_stop"})
            deadline = time.monotonic() + 6
            while True:
                packet = ws.portal.call(_receive, ws, deadline)
                if "text" in packet and json.loads(packet["text"]).get("state") == "off":
                    break

        expected_history = []
        for index, (events, audio, rate) in enumerate(exchanges):
            transcript, = [evt["text"] for evt in events if evt["type"] == "stt_final"]
            expected_words = ("brown fox", "lazy dog") if index == 0 else ("what", "fox")
            assert all(word in transcript.lower() for word in expected_words), transcript
            kinds = [event["type"] for event in events]
            assert kinds.index("stt_final") < kinds.index("token") < kinds.index("turn_done")
            bubble, = [evt for evt in events if evt["type"] == "message" and evt["role"] == "user"]
            assert bubble["content"] == transcript
            assert events.index(bubble) < kinds.index("token")
            reply = REPLIES[index]
            assert "".join(evt["chunk"] for evt in events if evt["type"] == "token") == reply
            prompt = model.chat_stream.call_args_list[index].args[0]
            assert transcript in prompt[-1]["content"]
            if index == 1:
                assert any(m["role"] == "assistant" and REPLIES[0] in m["content"] for m in prompt)
            expected_history.extend([("user", transcript), ("assistant", reply)])
            if real_speech:
                pcm = np.frombuffer(audio, "<i2").astype(float) / 32768
                reply_path = tmp_path / f"received-{index}.wav"
                session.mic_source.write_wav(resample_poly(pcm, 16000, rate), reply_path)
                spoken = session._realtime_stt.transcribe(reply_path)
                record_property(f"received_transcript_{index + 1}", spoken)
                # The wire text above must match exactly. For the independent
                # acoustic readback, require the complete subject and predicate
                # while allowing a different introductory article or demonstrative.
                expected = reply.removeprefix("The ").rstrip(".").lower()
                assert re.fullmatch(
                    rf"(?:(?:the|that|this|a) )?{re.escape(expected)}[.!?]*",
                    spoken.strip().lower(),
                ), spoken
        assert model.chat_stream.call_count == 2
        rows = db.get_messages(session.session_key)
        assert [(r.role, r.content) for r in rows] == expected_history
        if not real_speech:
            assert engine.spoken == list(REPLIES)
            assert len(engine.captures) == 2
            assert not any(path.exists() for path in engine.captures)
