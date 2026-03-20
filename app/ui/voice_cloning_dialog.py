"""Standalone Voice Cloning dialog for Pocket TTS."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)


_VOICES_DIR = Path(__file__).resolve().parents[2] / "voices"
_DEFAULT_TEST_PHRASE = "Hello, this is a preview of the cloned voice."
_REFINEMENT_PHRASE = (
    "The quick brown fox jumps over the lazy dog. "
    "She sells seashells by the seashore. "
    "How much wood would a woodchuck chuck if a woodchuck could chuck wood?"
)


def _ensure_ffmpeg_on_path() -> None:
    """Find ffmpeg and add its directory to PATH so pydub can use it."""
    if shutil.which("ffmpeg"):
        return
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
        Path("C:/ffmpeg/bin"),
        Path("C:/Program Files/ffmpeg/bin"),
    ]
    for d in candidates:
        if (d / "ffmpeg.exe").exists():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            return


def _write_wav_int16(path: str, sample_rate: int, audio_tensor) -> None:
    """Write audio tensor as 16-bit PCM WAV (avoids IEEE float format 3 errors)."""
    import numpy as np
    import scipy.io.wavfile
    data = audio_tensor.numpy() if hasattr(audio_tensor, "numpy") else audio_tensor
    data = np.clip(data, -1.0, 1.0)
    scipy.io.wavfile.write(path, sample_rate, (data * 32767).astype(np.int16))


class _WorkerSignals(QObject):
    status = Signal(str)
    done = Signal(object)
    error = Signal(str)


class VoiceCloningDialog(QDialog):
    """Upload audio, preview cloned voice, save/manage voice embeddings."""

    def __init__(self, session_controller, parent=None) -> None:
        super().__init__(parent)
        self._session = session_controller
        self._signals = _WorkerSignals()
        self._signals.status.connect(self._on_status)
        self._signals.done.connect(self._on_done)
        self._signals.error.connect(self._on_error)
        self._pending_voice_state: dict | None = None
        self._refine_source_path: str | None = None
        self._busy = False

        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)
        self.setWindowTitle("Voice Cloning (Pocket TTS)")
        self.resize(640, 620)
        self._build_ui()
        self._refresh_saved_voices()

    # ── UI Construction ──

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ── Voice source ──
        source_group = QGroupBox("Voice Source")
        source_layout = QVBoxLayout(source_group)

        radio_row = QHBoxLayout()
        self._radio_new = QRadioButton("New from audio files")
        self._radio_refine = QRadioButton("Refine saved voice:")
        self._radio_new.setChecked(True)
        radio_row.addWidget(self._radio_new)
        radio_row.addWidget(self._radio_refine)
        self._refine_combo = QComboBox()
        self._refine_combo.setMinimumWidth(160)
        self._refine_combo.setEnabled(False)
        radio_row.addWidget(self._refine_combo)
        radio_row.addStretch()
        self._radio_refine.toggled.connect(self._refine_combo.setEnabled)
        source_layout.addLayout(radio_row)

        # Audio files (always visible)
        tip = QLabel(
            "Add clean audio with 10-30s of speech. Background noise reduces quality."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #64748b; font-size: 11px; padding: 2px;")
        source_layout.addWidget(tip)

        self._file_list = QListWidget()
        self._file_list.setMaximumHeight(72)
        source_layout.addWidget(self._file_list)

        file_row = QHBoxLayout()
        self._add_btn = QPushButton("Add Files...")
        self._add_btn.clicked.connect(self._add_files)
        file_row.addWidget(self._add_btn)
        self._remove_btn = QPushButton("Remove")
        self._remove_btn.clicked.connect(self._remove_selected)
        file_row.addWidget(self._remove_btn)
        self._duration_label = QLabel("")
        file_row.addWidget(self._duration_label)
        file_row.addStretch()
        source_layout.addLayout(file_row)
        layout.addWidget(source_group)

        # ── Quality settings ──
        quality_group = QGroupBox("Quality")
        quality_layout = QHBoxLayout(quality_group)

        quality_layout.addWidget(QLabel("Refinement passes:"))
        self._passes_spin = QSpinBox()
        self._passes_spin.setRange(1, 5)
        self._passes_spin.setValue(3)
        self._passes_spin.setToolTip(
            "1 = direct clone (fast). Higher = re-synthesize and re-extract\n"
            "the voice iteratively, stripping recording artifacts.\n"
            "2-3 is usually optimal. Each extra pass adds ~10-15s."
        )
        quality_layout.addWidget(self._passes_spin)

        quality_layout.addSpacing(16)
        quality_layout.addWidget(QLabel("Decode steps:"))
        self._decode_steps_spin = QSpinBox()
        self._decode_steps_spin.setRange(1, 10)
        self._decode_steps_spin.setValue(5)
        self._decode_steps_spin.setToolTip(
            "LSD decode steps per audio frame.\n"
            "Higher = better quality but slower.\n"
            "1 = fastest, 3-5 = noticeably cleaner."
        )
        quality_layout.addWidget(self._decode_steps_spin)
        quality_layout.addStretch()
        layout.addWidget(quality_group)

        # ── Preview & save ──
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        self._phrase_edit = QLineEdit(_DEFAULT_TEST_PHRASE)
        preview_layout.addWidget(self._phrase_edit)

        action_row = QHBoxLayout()
        self._preview_btn = QPushButton("Preview")
        self._preview_btn.clicked.connect(self._preview)
        action_row.addWidget(self._preview_btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._stop_playback)
        action_row.addWidget(self._stop_btn)
        self._save_btn = QPushButton("Save Voice As...")
        self._save_btn.clicked.connect(self._save_voice)
        action_row.addWidget(self._save_btn)
        preview_layout.addLayout(action_row)

        self._status_label = QLabel("Status: Ready")
        self._status_label.setStyleSheet("font-weight: bold;")
        preview_layout.addWidget(self._status_label)
        layout.addWidget(preview_group)

        # ── Saved voices management ──
        saved_group = QGroupBox("Saved Voices")
        saved_layout = QVBoxLayout(saved_group)
        self._saved_list = QListWidget()
        self._saved_list.setMaximumHeight(100)
        saved_layout.addWidget(self._saved_list)

        saved_row = QHBoxLayout()
        self._test_saved_btn = QPushButton("Test")
        self._test_saved_btn.clicked.connect(self._test_saved)
        saved_row.addWidget(self._test_saved_btn)
        self._delete_saved_btn = QPushButton("Delete")
        self._delete_saved_btn.clicked.connect(self._delete_saved)
        saved_row.addWidget(self._delete_saved_btn)
        self._use_btn = QPushButton("Use Selected Voice")
        self._use_btn.clicked.connect(self._use_selected)
        saved_row.addWidget(self._use_btn)
        saved_row.addStretch()
        saved_layout.addLayout(saved_row)
        layout.addWidget(saved_group)

    # ── File management ──

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select audio files",
            "",
            "Audio files (*.wav *.mp3 *.ogg *.flac);;All files (*)",
        )
        for f in files:
            self._file_list.addItem(os.path.normpath(f))
        if files:
            self._update_duration()

    def _remove_selected(self) -> None:
        for item in self._file_list.selectedItems():
            self._file_list.takeItem(self._file_list.row(item))
        self._update_duration()

    def _get_file_paths(self) -> list[str]:
        return [self._file_list.item(i).text() for i in range(self._file_list.count())]

    def _update_duration(self) -> None:
        paths = self._get_file_paths()
        if not paths:
            self._duration_label.setText("")
            return
        try:
            _ensure_ffmpeg_on_path()
            from pydub import AudioSegment
            total_ms = sum(len(AudioSegment.from_file(p)) for p in paths)
            secs = total_ms / 1000.0
            label = f"Duration: {secs:.1f}s"
            if secs < 10:
                label += "  (short — 10s+ recommended)"
            elif secs > 30:
                label += "  (first 30s used)"
            self._duration_label.setText(label)
        except ImportError:
            self._duration_label.setText("(install pydub for duration info)")
        except Exception as exc:
            self._duration_label.setText(f"Error: {exc}")

    def _merge_to_wav(self, paths: list[str]) -> str:
        """Merge multiple audio files into a single temp WAV."""
        _ensure_ffmpeg_on_path()
        from pydub import AudioSegment
        combined = AudioSegment.from_file(paths[0])
        for p in paths[1:]:
            combined = combined + AudioSegment.from_file(p)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        combined.export(tmp.name, format="wav")
        return tmp.name

    # ── Unified preview ──

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for btn in (
            self._preview_btn, self._save_btn, self._add_btn,
            self._test_saved_btn,
        ):
            btn.setEnabled(not busy)

    def _preview(self) -> None:
        phrase = self._phrase_edit.text().strip()
        if not phrase:
            QMessageBox.warning(self, "No phrase", "Enter a test phrase.")
            return

        passes = self._passes_spin.value()
        decode_steps = self._decode_steps_spin.value()
        audio_paths = self._get_file_paths()

        if self._radio_refine.isChecked():
            safetensors_path = self._refine_combo.currentData()
            if not safetensors_path:
                QMessageBox.warning(self, "No voice", "No saved voice selected to refine.")
                return
            self._refine_source_path = safetensors_path
            self._set_busy(True)
            threading.Thread(
                target=self._preview_from_saved,
                args=(safetensors_path, audio_paths, phrase, passes, decode_steps),
                daemon=True,
            ).start()
        else:
            if not audio_paths:
                QMessageBox.warning(self, "No files", "Add audio files first.")
                return
            self._refine_source_path = None
            self._set_busy(True)
            threading.Thread(
                target=self._preview_from_audio,
                args=(audio_paths, phrase, passes, decode_steps),
                daemon=True,
            ).start()

    def _preview_from_audio(
        self, paths: list[str], phrase: str, passes: int, decode_steps: int,
    ) -> None:
        tmp_path: str | None = None
        try:
            if len(paths) > 1:
                self._signals.status.emit("Merging audio files...")
                tmp_path = self._merge_to_wav(paths)
                wav_path = tmp_path
            else:
                wav_path = paths[0]

            self._signals.status.emit("Processing voice...")
            model = self._get_pocket_model(decode_steps)
            if model is None:
                self._signals.error.emit(
                    "Pocket TTS model not available. Is pocket-tts installed and provider set?"
                )
                return

            voice_state = model.get_state_for_audio_prompt(wav_path)
            voice_state = self._run_refinement_passes(model, voice_state, passes)
            self._pending_voice_state = voice_state

            self._signals.status.emit("Generating preview...")
            audio = model.generate_audio(voice_state, phrase, copy_state=True)

            self._signals.status.emit("Playing...")
            import sounddevice as sd
            sd.play(audio.numpy(), model.sample_rate)
            sd.wait()
            self._signals.done.emit(None)
        except Exception as exc:
            self._signals.error.emit(str(exc))
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _preview_from_saved(
        self,
        safetensors_path: str,
        audio_paths: list[str],
        phrase: str,
        passes: int,
        decode_steps: int,
    ) -> None:
        tmp_path: str | None = None
        try:
            model = self._get_pocket_model(decode_steps)
            if model is None:
                self._signals.error.emit("Pocket TTS model not available.")
                return

            if audio_paths:
                self._signals.status.emit("Processing audio samples...")
                if len(audio_paths) > 1:
                    tmp_path = self._merge_to_wav(audio_paths)
                    wav = tmp_path
                else:
                    wav = audio_paths[0]
                voice_state = model.get_state_for_audio_prompt(wav)
            else:
                self._signals.status.emit("Loading saved voice...")
                voice_state = model.get_state_for_audio_prompt(safetensors_path)

            voice_state = self._run_refinement_passes(model, voice_state, passes)
            self._pending_voice_state = voice_state

            self._signals.status.emit("Generating preview...")
            audio = model.generate_audio(voice_state, phrase, copy_state=True)

            self._signals.status.emit("Playing...")
            import sounddevice as sd
            sd.play(audio.numpy(), model.sample_rate)
            sd.wait()
            self._signals.done.emit(None)
        except Exception as exc:
            self._signals.error.emit(str(exc))
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ── Shared helpers ──

    def _run_refinement_passes(self, model, voice_state: dict, passes: int) -> dict:
        """Run self-distillation passes: generate -> re-extract voice state."""
        for p in range(1, passes):
            self._signals.status.emit(f"Refinement pass {p}/{passes - 1}...")
            synth_audio = model.generate_audio(
                voice_state, _REFINEMENT_PHRASE, copy_state=True
            )
            synth_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            synth_tmp.close()
            try:
                _write_wav_int16(synth_tmp.name, model.sample_rate, synth_audio)
                voice_state = model.get_state_for_audio_prompt(synth_tmp.name)
            finally:
                try:
                    os.unlink(synth_tmp.name)
                except OSError:
                    pass
        return voice_state

    def _get_pocket_model(self, decode_steps: int = 1):
        if decode_steps <= 1:
            tts = getattr(self._session, "_tts", None)
            if tts is not None and hasattr(tts, "get_model"):
                return tts.get_model()
        try:
            from pocket_tts import TTSModel
            return TTSModel.load_model(lsd_decode_steps=max(1, decode_steps))
        except Exception:
            return None

    def _stop_playback(self) -> None:
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    # ── Status / done / error ──

    def _on_status(self, msg: str) -> None:
        self._status_label.setText(f"Status: {msg}")

    def _on_done(self, _result) -> None:
        self._set_busy(False)
        src = self._refine_source_path
        if src:
            name = Path(src).stem
            self._status_label.setText(
                f'Status: Done — click "Save Voice As..." or overwrite "{name}"'
            )
        else:
            self._status_label.setText('Status: Done — click "Save Voice As..." to keep')

    def _on_error(self, msg: str) -> None:
        self._status_label.setText("Status: Error")
        self._set_busy(False)
        QMessageBox.warning(self, "Error", msg)

    # ── Save voice ──

    def _save_voice(self) -> None:
        if self._pending_voice_state is None:
            QMessageBox.information(
                self, "Preview first", "Click Preview to process a voice before saving."
            )
            return

        if self._refine_source_path:
            orig_name = Path(self._refine_source_path).stem
            reply = QMessageBox.question(
                self,
                "Save refined voice",
                f'Overwrite "{orig_name}" with the refined version?\n\n'
                'Click "No" to save as a new voice instead.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._do_save(self._refine_source_path)
                return
            elif reply == QMessageBox.StandardButton.Cancel:
                return

        name, ok = QInputDialog.getText(self, "Save Voice", "Voice name:")
        if not ok or not name.strip():
            return
        safe_name = "".join(
            c for c in name.strip() if c.isalnum() or c in " _-"
        ).strip()
        if not safe_name:
            return
        _VOICES_DIR.mkdir(parents=True, exist_ok=True)
        self._do_save(str(_VOICES_DIR / f"{safe_name}.safetensors"))

    def _do_save(self, dest_path: str) -> None:
        try:
            from app.tts.pocket_tts_service import PocketTtsService
            dest = Path(dest_path)
            if dest.exists():
                tmp = dest.with_suffix(".safetensors.tmp")
                PocketTtsService.export_voice(self._pending_voice_state, str(tmp))
                self._release_voice_mmap(dest_path)
                tmp.replace(dest)
                self._reload_voice_if_active(dest_path)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                PocketTtsService.export_voice(self._pending_voice_state, dest_path)
            self._status_label.setText(f"Saved: {dest.name}")
            self._refresh_saved_voices()
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    def _release_voice_mmap(self, path: str) -> None:
        """Switch TTS to a builtin voice so the mmap on path is released."""
        tts = getattr(self._session, "_tts", None)
        if tts is None:
            return
        current_voice = getattr(getattr(tts, "_settings", None), "pocket_tts_voice", "")
        if current_voice == Path(path).name:
            set_voice = getattr(tts, "set_voice", None)
            if callable(set_voice):
                set_voice("alba")

    def _reload_voice_if_active(self, path: str) -> None:
        """Reload the voice file after it was replaced on disk."""
        tts = getattr(self._session, "_tts", None)
        if tts is None:
            return
        set_voice = getattr(tts, "set_voice", None)
        if callable(set_voice):
            set_voice(Path(path).name)

    # ── Saved voices management ──

    def _refresh_saved_voices(self) -> None:
        self._saved_list.clear()
        self._refine_combo.clear()
        if not _VOICES_DIR.is_dir():
            return
        for f in sorted(_VOICES_DIR.iterdir()):
            if f.suffix == ".safetensors":
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
                except Exception:
                    mtime = ""
                label = f"{f.stem}  ({mtime})"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, str(f))
                self._saved_list.addItem(item)
                self._refine_combo.addItem(f.stem, str(f))

    def _test_saved(self) -> None:
        item = self._saved_list.currentItem()
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        phrase = self._phrase_edit.text().strip() or _DEFAULT_TEST_PHRASE
        self._set_busy(True)
        threading.Thread(
            target=self._test_saved_worker, args=(path, phrase), daemon=True
        ).start()

    def _test_saved_worker(self, safetensors_path: str, phrase: str) -> None:
        try:
            self._signals.status.emit("Loading voice...")
            model = self._get_pocket_model()
            if model is None:
                self._signals.error.emit("Pocket TTS model not available.")
                return
            voice_state = model.get_state_for_audio_prompt(safetensors_path)
            self._signals.status.emit("Generating audio...")
            audio = model.generate_audio(voice_state, phrase, copy_state=True)
            self._signals.status.emit("Playing...")
            import sounddevice as sd
            sd.play(audio.numpy(), model.sample_rate)
            sd.wait()
            self._refine_source_path = None
            self._signals.done.emit(None)
        except Exception as exc:
            self._signals.error.emit(str(exc))

    def _delete_saved(self) -> None:
        item = self._saved_list.currentItem()
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        reply = QMessageBox.question(
            self, "Delete voice", f"Delete {path.stem}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                path.unlink()
            except Exception as exc:
                QMessageBox.warning(self, "Error", str(exc))
                return
            self._refresh_saved_voices()

    def _use_selected(self) -> None:
        item = self._saved_list.currentItem()
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        name = Path(path).name
        self._session.set_tts_voice(name)
        self._status_label.setText(f"Active voice set to: {name}")

    def closeEvent(self, event) -> None:
        self._stop_playback()
        super().closeEvent(event)
