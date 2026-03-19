"""Style-Bert-VITS2 TTS backend -- emotion-aware speech synthesis."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import json
import logging
import threading

from app.core.settings import TtsSettings

log = logging.getLogger(__name__)

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]

_sbv2_available = False
try:
    from style_bert_vits2.tts_model import TTSModel
    from style_bert_vits2.nlp import bert_models
    from style_bert_vits2.constants import Languages
    _sbv2_available = True
except ImportError:
    TTSModel = None  # type: ignore[assignment,misc]
    bert_models = None  # type: ignore[assignment]
    Languages = None  # type: ignore[assignment,misc]

_REACTION_TO_STYLE: dict[str, str] = {
    "excited": "Happy",
    "enthusiastic": "Happy",
    "cheerful": "Happy",
    "friendly": "Happy",
    "surprised": "Surprise",
    "sad": "Sad",
    "angry": "Angry",
    "calm": "Neutral",
    "serious": "Neutral",
    "gentle": "Neutral",
    "neutral": "Neutral",
}

_REACTION_SPEED: dict[str, float] = {
    "excited": 1.1,
    "enthusiastic": 1.08,
    "cheerful": 1.08,
    "angry": 1.05,
    "surprised": 1.05,
    "friendly": 1.02,
    "neutral": 1.0,
    "calm": 0.95,
    "serious": 0.95,
    "sad": 0.92,
    "gentle": 0.92,
}

_LANGUAGE_MAP: dict[str, object] = {}


def _get_language(lang_str: str) -> object:
    """Resolve a language string to the SBV2 Languages enum value."""
    if not _sbv2_available or Languages is None:
        return None
    if not _LANGUAGE_MAP:
        for attr in ("EN", "JP", "ZH"):
            val = getattr(Languages, attr, None)
            if val is not None:
                _LANGUAGE_MAP[attr] = val
    return _LANGUAGE_MAP.get(lang_str.upper().strip(), _LANGUAGE_MAP.get("EN"))


_BERT_MODELS: dict[str, str] = {
    "EN": "microsoft/deberta-v3-large",
    "JP": "ku-nlp/deberta-v2-large-japanese-char-wwm",
    "ZH": "hfl/chinese-roberta-wwm-ext-large",
}


class StyleBertVits2TtsService:
    """TTS using Style-Bert-VITS2 with emotional style control."""

    def __init__(self, settings: TtsSettings, output_device: int | None = None) -> None:
        self._settings = settings
        self._output_device = output_device
        self._lock = threading.Lock()
        self._model: object | None = None
        self._available_styles: list[str] = ["Neutral"]
        self._available_speakers: list[str] = []
        self._last_error: str | None = None
        self._stop_requested = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._loaded = threading.Event()

        if not _sbv2_available or np is None or sd is None:
            missing: list[str] = []
            if not _sbv2_available:
                missing.append("style-bert-vits2")
            if np is None:
                missing.append("numpy")
            if sd is None:
                missing.append("sounddevice")
            self._last_error = f"Missing dependencies: {', '.join(missing)}. pip install {' '.join(missing)}"
            self._loaded.set()
        else:
            threading.Thread(target=self._load_model, daemon=True, name="sbv2-load").start()

    def _resolve_model_dir(self) -> Path:
        base = Path(__file__).resolve().parents[2]
        raw = getattr(self._settings, "sbv2_model_path", "") or ""
        if not raw:
            return base / "models" / "style-bert-vits2"
        p = Path(raw)
        return p if p.is_absolute() else (base / p)

    def _load_model(self) -> None:
        try:
            model_dir = self._resolve_model_dir()
            config_path = model_dir / "config.json"
            style_vec_path = model_dir / "style_vectors.npy"

            if not config_path.exists():
                self._last_error = f"SBV2 config not found: {config_path}"
                self._loaded.set()
                return

            safetensors = list(model_dir.glob("*.safetensors"))
            if not safetensors:
                self._last_error = f"No .safetensors model found in {model_dir}"
                self._loaded.set()
                return

            model_path = safetensors[0]

            lang_str = getattr(self._settings, "sbv2_language", "EN") or "EN"
            device = getattr(self._settings, "sbv2_device", "cpu") or "cpu"

            lang = _get_language(lang_str)
            bert_model_name = _BERT_MODELS.get(lang_str.upper(), _BERT_MODELS["EN"])

            log.info("Loading SBV2 BERT model: %s for language %s", bert_model_name, lang_str)
            bert_models.load_model(lang, bert_model_name)
            bert_models.load_tokenizer(lang, bert_model_name)

            model = TTSModel(
                model_path=model_path,
                config_path=config_path,
                style_vec_path=style_vec_path if style_vec_path.exists() else None,
                device=device,
            )

            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                data = cfg.get("data", {})
                self._available_styles = list(data.get("style2id", {}).keys()) or ["Neutral"]
                self._available_speakers = list(data.get("spk2id", {}).keys())
            except Exception:
                pass

            with self._lock:
                self._model = model
            self._last_error = None
            log.info("SBV2 model loaded from %s (%d styles, %d speakers)",
                     model_dir, len(self._available_styles), len(self._available_speakers))
        except Exception as exc:
            self._last_error = f"SBV2 load failed: {exc}"
            log.warning("SBV2 load failed: %s", exc)
        finally:
            self._loaded.set()

    # -- Protocol methods --------------------------------------------------

    def get_status(self) -> tuple[str, str]:
        if not self._settings.enabled:
            return "disabled", "TTS disabled"
        if self._last_error:
            return "error", self._last_error
        self._loaded.wait(timeout=0.5)
        with self._lock:
            if self._model is None:
                return "error", self._last_error or "Model not loaded"
        return "ready", "Style-Bert-VITS2 TTS ready"

    def warmup_sync(self) -> bool:
        if not self._settings.enabled:
            return True
        if not self._loaded.wait(timeout=120.0):
            self._last_error = "SBV2 load timed out"
            return False
        with self._lock:
            return self._model is not None

    def warmup_async(self) -> None:
        self._loaded.wait(timeout=30.0)

    def stop(self) -> None:
        self._stop_requested.set()

    def set_output_device(self, device_index: int | None) -> None:
        self._output_device = device_index

    def list_voices(self) -> list[str]:
        if self._available_speakers:
            return list(self._available_speakers)
        return ["default"]

    def reaction_to_speed(self, reaction: str | None) -> float:
        if not (reaction or "").strip():
            return 1.0
        return _REACTION_SPEED.get((reaction or "").strip().lower(), 1.0)

    def _resolve_style(self, reaction: str | None) -> tuple[str, float]:
        """Map a reaction tag to (style_name, style_weight)."""
        default_style = getattr(self._settings, "sbv2_style", "Neutral") or "Neutral"
        default_weight = getattr(self._settings, "sbv2_style_weight", 1.0) or 1.0

        if not reaction:
            return default_style, float(default_weight)

        mapped = _REACTION_TO_STYLE.get(reaction.strip().lower(), default_style)
        if mapped not in self._available_styles:
            mapped = default_style
        if mapped not in self._available_styles and self._available_styles:
            mapped = self._available_styles[0]

        return mapped, float(default_weight)

    def speak_async(
        self,
        text: str,
        reaction: str | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if not self._settings.enabled or not (text or "").strip():
            return
        self._stop_requested.clear()
        self._speech_thread = threading.Thread(
            target=self._speak_worker,
            args=(text.strip(), on_done, reaction),
            daemon=True,
        )
        self._speech_thread.start()

    # -- Audio generation --------------------------------------------------

    def generate_audio(self, text: str, speed: float = 1.0, reaction: str | None = None) -> tuple | None:
        """Run SBV2 inference, returning (audio_float32, sample_rate) or None."""
        if not self._loaded.wait(timeout=30.0):
            return None
        with self._lock:
            model = self._model
        if model is None or np is None:
            return None

        try:
            lang_str = getattr(self._settings, "sbv2_language", "EN") or "EN"
            lang = _get_language(lang_str)
            style_name, style_weight = self._resolve_style(reaction)

            sr, audio_int16 = model.infer(
                text=text,
                language=lang,
                style=style_name,
                style_weight=style_weight,
            )
            audio = np.asarray(audio_int16, dtype=np.float32)
            if audio.dtype == np.int16 or audio.max() > 2.0:
                audio = audio / 32768.0
            if audio.size == 0:
                return None
            return audio, sr
        except Exception as exc:
            log.warning("SBV2 generate failed: %s", exc)
            return None

    def _speak_worker(
        self,
        text: str,
        on_done: Callable[[], None] | None = None,
        reaction: str | None = None,
    ) -> None:
        try:
            if sd is None:
                return
            result = self.generate_audio(text, reaction=reaction)
            if result is None or self._stop_requested.is_set():
                return
            audio_data, sample_rate = result
            silence_samples = int(sample_rate * 0.15)
            silence = np.zeros(silence_samples, dtype=np.float32)
            audio_data = np.concatenate([audio_data, silence])
            sd.play(
                audio_data.reshape(-1, 1),
                sample_rate,
                device=self._output_device,
            )
            sd.wait()
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass

    def has_pending_audio(self) -> bool:
        return self._speech_thread is not None and self._speech_thread.is_alive()
