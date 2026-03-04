from __future__ import annotations

import re
from pathlib import Path
import tempfile
import threading
import winsound

from app.core.settings import TtsSettings


class LlasaTtsService:
    def __init__(self, settings: TtsSettings) -> None:
        self._settings = settings
        self._stop_event = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._warmup_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status_lock = threading.Lock()

        self._torch = None
        self._soundfile = None
        self._tokenizer = None
        self._model = None
        self._codec_model = None
        self._device = settings.llasa_device
        self._model_device = settings.llasa_device
        self._codec_device = settings.llasa_device
        self._load_error: str | None = None
        self._status = "not_initialized"
        self._status_message = "Waiting for first Llasa synthesis"

    def get_status(self) -> tuple[str, str]:
        with self._status_lock:
            return self._status, self._status_message

    def _set_status(self, status: str, message: str) -> None:
        with self._status_lock:
            self._status = status
            self._status_message = message

    def speak_async(self, text: str) -> None:
        if not self._settings.enabled:
            return
        if not text.strip():
            return

        try:
            self.stop()
            self._stop_event.clear()
            self._speech_thread = threading.Thread(target=self._speak_worker, args=(text,), daemon=True)
            self._speech_thread.start()
        except Exception as exc:
            self._set_status("error", f"Failed to start Llasa TTS worker: {exc}")

    def warmup_async(self) -> None:
        if not self._settings.enabled:
            return
        with self._lock:
            if self._warmup_thread is not None and self._warmup_thread.is_alive():
                return
            if self._model is not None and self._tokenizer is not None and self._codec_model is not None:
                self._set_status("ready", f"Initialized on {self._device}")
                return
            self._warmup_thread = threading.Thread(target=self._warmup_worker, daemon=True)
            self._warmup_thread.start()

    def warmup_sync(self) -> bool:
        if not self._settings.enabled:
            return True
        return self._ensure_models_loaded()

    def _warmup_worker(self) -> None:
        self._ensure_models_loaded()

    def _apply_cuda_memory_limit(self, torch) -> None:
        cap_mb = max(0, int(getattr(self._settings, "llasa_max_vram_mb", 0)))
        if cap_mb <= 0:
            return
        if not torch.cuda.is_available():
            return

        try:
            device_index = torch.cuda.current_device()
            props = torch.cuda.get_device_properties(device_index)
            total_bytes = int(getattr(props, "total_memory", 0))
            if total_bytes <= 0:
                return

            cap_bytes = cap_mb * 1024 * 1024
            fraction = max(0.05, min(float(cap_bytes) / float(total_bytes), 0.98))
            torch.cuda.set_per_process_memory_fraction(fraction, device=device_index)
            self._set_status(
                "loading",
                f"Loading Llasa models with VRAM cap {cap_mb}MB ({fraction * 100:.1f}% of GPU)",
            )
        except Exception:
            return

    def stop(self) -> None:
        self._stop_event.set()
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def _speak_worker(self, text: str) -> None:
        waveform = self._synthesize(text)
        if waveform is None or self._stop_event.is_set():
            if self._load_error:
                self._set_status("error", self._load_error)
            else:
                self._set_status("error", "No waveform generated from Llasa output")
            return

        wav_path = Path(tempfile.mkstemp(suffix=".wav", prefix="assistant_llasa_tts_")[1])
        try:
            assert self._soundfile is not None
            self._soundfile.write(str(wav_path), waveform, 16000)
            if self._stop_event.is_set():
                return
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._set_status(
                "ready",
                f"Initialized model={self._model_device} codec={self._codec_device}",
            )
        except Exception:
            self._set_status("error", "Failed to write or play generated audio")
            return

    def _synthesize(self, text: str):
        if not self._ensure_models_loaded():
            return None

        assert self._torch is not None
        assert self._tokenizer is not None
        assert self._model is not None
        assert self._codec_model is not None

        formatted_text = f"<|TEXT_UNDERSTANDING_START|>{text}<|TEXT_UNDERSTANDING_END|>"
        chat = [
            {"role": "user", "content": "Convert the text to speech:" + formatted_text},
            {"role": "assistant", "content": "<|SPEECH_GENERATION_START|>"},
        ]

        try:
            with self._torch.no_grad():
                input_ids = self._tokenizer.apply_chat_template(
                    chat,
                    tokenize=True,
                    return_tensors="pt",
                    continue_final_message=True,
                )
                input_ids = input_ids.to(self._model_device)
                speech_end_id = self._tokenizer.convert_tokens_to_ids("<|SPEECH_GENERATION_END|>")

                if str(self._model_device).startswith("cuda"):
                    with self._torch.autocast(device_type="cuda", dtype=self._torch.float16):
                        outputs = self._model.generate(
                            input_ids,
                            max_length=int(self._settings.llasa_max_length),
                            eos_token_id=speech_end_id,
                            do_sample=True,
                            top_p=float(self._settings.llasa_top_p),
                            temperature=float(self._settings.llasa_temperature),
                        )
                else:
                    outputs = self._model.generate(
                        input_ids,
                        max_length=int(self._settings.llasa_max_length),
                        eos_token_id=speech_end_id,
                        do_sample=True,
                        top_p=float(self._settings.llasa_top_p),
                        temperature=float(self._settings.llasa_temperature),
                    )

                generated_ids = outputs[0][input_ids.shape[1] :]
                if len(generated_ids) > 0 and int(generated_ids[-1].item()) == int(speech_end_id):
                    generated_ids = generated_ids[:-1]
                speech_tokens_text = self._tokenizer.batch_decode(
                    generated_ids,
                    skip_special_tokens=False,
                )
                speech_ids = self._extract_speech_ids("".join(speech_tokens_text))
                if not speech_ids:
                    return None

                speech_tokens = self._torch.tensor(speech_ids, device=self._codec_device).unsqueeze(0).unsqueeze(0)
                gen_wav = self._codec_model.decode_code(speech_tokens)
                return gen_wav[0, 0, :].detach().cpu().float().numpy()
        except Exception:
            return None

    def _ensure_models_loaded(self) -> bool:
        if self._model is not None and self._tokenizer is not None and self._codec_model is not None:
            self._set_status(
                "ready",
                f"Initialized model={self._model_device} codec={self._codec_device}",
            )
            return True

        with self._lock:
            if self._model is not None and self._tokenizer is not None and self._codec_model is not None:
                self._set_status(
                    "ready",
                    f"Initialized model={self._model_device} codec={self._codec_device}",
                )
                return True
            if self._load_error is not None:
                self._set_status("error", self._load_error)
                return False

            try:
                self._set_status("loading", "Loading Llasa and codec models...")
                import torch  # type: ignore[import-not-found]
                import soundfile as sf  # type: ignore[import-not-found]
                from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer  # type: ignore[import-not-found]

                try:
                    import sympy  # type: ignore[import-not-found]

                    if not hasattr(sympy, "printing"):
                        import sympy.printing as sympy_printing  # type: ignore[import-not-found]

                        setattr(sympy, "printing", sympy_printing)
                except Exception:
                    pass

                self._torch = torch
                self._soundfile = sf

                device = self._settings.llasa_device.strip().lower() or "cuda"
                if device == "cuda" and not torch.cuda.is_available():
                    device = "cpu"
                preferred_device = device
                cap_mb = max(0, int(getattr(self._settings, "llasa_max_vram_mb", 0)))
                low_vram_split_mode = preferred_device == "cuda" and cap_mb > 0 and cap_mb <= 8192

                def load_for_devices(model_device: str, codec_device: str) -> None:
                    model_kwargs: dict[str, object] = {"low_cpu_mem_usage": True}
                    codec_kwargs: dict[str, object] = {"low_cpu_mem_usage": True}
                    if model_device.startswith("cuda"):
                        model_kwargs["dtype"] = torch.float16
                    if codec_device.startswith("cuda"):
                        codec_kwargs["dtype"] = torch.float16

                    self._tokenizer = AutoTokenizer.from_pretrained(self._settings.llasa_model)
                    self._model = AutoModelForCausalLM.from_pretrained(
                        self._settings.llasa_model,
                        **model_kwargs,
                    )
                    self._model.eval()
                    self._model.to(model_device)

                    try:
                        self._codec_model = AutoModel.from_pretrained(
                            self._settings.llasa_codec_model,
                            trust_remote_code=True,
                            **codec_kwargs,
                        )
                    except Exception:
                        from xcodec2.modeling_xcodec2 import XCodec2Model  # type: ignore[import-not-found]

                        self._codec_model = XCodec2Model.from_pretrained(
                            self._settings.llasa_codec_model,
                            **codec_kwargs,
                        )
                    self._codec_model.eval()
                    self._codec_model.to(codec_device)

                if preferred_device == "cuda":
                    self._apply_cuda_memory_limit(torch)

                try:
                    if low_vram_split_mode:
                        self._set_status(
                            "loading",
                            f"Low-VRAM mode active ({cap_mb}MB): model on cuda, codec on cpu",
                        )
                        load_for_devices("cuda", "cpu")
                        self._model_device = "cuda"
                        self._codec_device = "cpu"
                    else:
                        load_for_devices(preferred_device, preferred_device)
                        self._model_device = preferred_device
                        self._codec_device = preferred_device

                    self._device = self._model_device
                    self._set_status(
                        "ready",
                        f"Initialized model={self._model_device} codec={self._codec_device}",
                    )
                    return True
                except Exception as first_exc:
                    self._model = None
                    self._tokenizer = None
                    self._codec_model = None

                    if preferred_device == "cuda":
                        try:
                            torch.cuda.empty_cache()
                        except Exception:
                            pass

                        self._set_status(
                            "loading",
                            "CUDA load failed (possibly VRAM cap/OOM). Falling back to CPU...",
                        )
                        try:
                            load_for_devices("cpu", "cpu")
                            self._model_device = "cpu"
                            self._codec_device = "cpu"
                            self._device = "cpu"
                            self._set_status(
                                "ready",
                                "Initialized model=cpu codec=cpu (GPU cap/OOM fallback)",
                            )
                            return True
                        except Exception as cpu_exc:
                            self._load_error = f"GPU load error: {first_exc}; CPU fallback error: {cpu_exc}"
                            self._set_status("error", self._load_error)
                            return False

                    self._load_error = str(first_exc)
                    self._set_status("error", self._load_error)
                    return False
            except Exception as exc:
                self._load_error = str(exc)
                self._set_status("error", self._load_error)
                return False

    @staticmethod
    def _extract_speech_ids(token_text: str) -> list[int]:
        values = re.findall(r"<\|s_(\d+)\|>", token_text)
        return [int(value) for value in values]
