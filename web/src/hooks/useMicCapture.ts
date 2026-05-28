/**
 * Glue hook between Zustand store state and the
 * :class:`AudioInputManager`.
 *
 * Responsibilities:
 *   - When *this* client owns the mic (``voiceMode !== "off"`` and the
 *     server's ``voiceOwnerId`` matches our ``clientId``), spin up the
 *     audio worklet and ship frames to the WebSocket.
 *   - Tear the capture down the moment we lose ownership, the mic is
 *     stopped, the WebSocket drops, or the device id / DSP toggles
 *     change.
 *   - Feed a normalised "VU-meter" RMS value into ``setAudioLevel`` so
 *     the existing mic-button pulse animation keeps working.
 */

import { useEffect, useRef } from "react";
import {
  AudioInputManager,
  type AudioInputConstraints,
} from "../audio/AudioInputManager";
import {
  getStoredDspPreferences,
  getStoredInputDeviceId,
} from "../audio/DeviceManager";
import { useAssistantStore } from "../store";

export interface UseMicCaptureOptions {
  sendBytes: (frame: Uint8Array) => void;
  /** Override the cached device / DSP prefs for testing. */
  constraints?: Partial<AudioInputConstraints>;
}

/**
 * Returns the active manager so callers can interrogate it (eg. force
 * a re-acquire when the user picks a new device). The hook owns its
 * lifecycle; the caller never needs to call ``start()`` / ``stop()``.
 */
export function useMicCapture(
  options: UseMicCaptureOptions,
): { manager: AudioInputManager | null } {
  const { sendBytes, constraints } = options;
  const managerRef = useRef<AudioInputManager | null>(null);
  const voiceMode = useAssistantStore((s) => s.voiceMode);
  const clientId = useAssistantStore((s) => s.clientId);
  const voiceOwnerId = useAssistantStore((s) => s.voiceOwnerId);
  const setAudioLevel = useAssistantStore((s) => s.setAudioLevel);
  const connectionStatus = useAssistantStore((s) => s.connection.status);

  const owned =
    voiceMode !== "off" &&
    !!clientId &&
    voiceOwnerId === clientId &&
    connectionStatus === "connected";

  useEffect(() => {
    if (!owned) {
      // Lost (or never claimed) ownership — tear any active manager down.
      if (managerRef.current) {
        void managerRef.current.stop();
        managerRef.current = null;
        setAudioLevel(0);
      }
      return;
    }
    const stored = getStoredInputDeviceId();
    const dsp = getStoredDspPreferences();
    const merged: AudioInputConstraints = {
      deviceId: stored || undefined,
      echoCancellation: dsp.echoCancellation,
      noiseSuppression: dsp.noiseSuppression,
      autoGainControl: dsp.autoGainControl,
      ...constraints,
    };
    const manager = new AudioInputManager({
      send: sendBytes,
      onLevel: (rms) => {
        // The button animation already clamps to 0-1, but the worklet
        // RMS sits well below 1.0 even on loud speech (we max out at
        // about 0.3 talking near the mic). Multiply by a fixed gain
        // so the pulse ring still moves visibly without hitting the
        // ceiling and clamp to [0, 1].
        setAudioLevel(Math.min(1, Math.max(0, rms * 3)));
      },
      onError: (err) => {
        console.warn("AudioInputManager error", err);
      },
    });
    manager.setConstraints(merged);
    managerRef.current = manager;
    void manager.start().catch((err) => {
      console.warn("Failed to start microphone capture", err);
    });
    return () => {
      void manager.stop();
      if (managerRef.current === manager) {
        managerRef.current = null;
      }
      setAudioLevel(0);
    };
    // ``constraints`` is shallow-compared by reference; callers that
    // pass an inline object should memoise it.
  }, [owned, sendBytes, constraints, setAudioLevel]);

  return { manager: managerRef.current };
}
