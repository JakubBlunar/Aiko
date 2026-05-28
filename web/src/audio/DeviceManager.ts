/**
 * Enumerates audio input/output devices in the browser and persists
 * the user's selection through `localStorage` so the next launch
 * remembers it.
 *
 * Device labels are only populated after the user grants microphone
 * permission. The helpers here cope with both states — pre-permission
 * (devices are `MediaDeviceInfo` stubs with empty labels) and
 * post-permission (full device list with labels).
 */

const STORAGE_KEY_INPUT = "assistant.audio.inputDeviceId";
const STORAGE_KEY_OUTPUT = "assistant.audio.outputDeviceId";
const STORAGE_KEY_DSP_EC = "assistant.audio.echoCancellation";
const STORAGE_KEY_DSP_NS = "assistant.audio.noiseSuppression";
const STORAGE_KEY_DSP_AGC = "assistant.audio.autoGainControl";

export interface DeviceListing {
  deviceId: string;
  label: string;
  groupId: string;
}

export interface DeviceLists {
  inputs: DeviceListing[];
  outputs: DeviceListing[];
}

export type MicPermissionState = "granted" | "denied" | "prompt" | "unknown";

const readBoolean = (key: string, fallback: boolean): boolean => {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return fallback;
    return raw === "true";
  } catch {
    return fallback;
  }
};

const writeBoolean = (key: string, value: boolean): void => {
  try {
    window.localStorage.setItem(key, value ? "true" : "false");
  } catch {
    /* private mode / quota — surface as a no-op */
  }
};

const readString = (key: string): string => {
  try {
    return window.localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
};

const writeString = (key: string, value: string): void => {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
};

/** Return the cached input device id (empty string = "default"). */
export function getStoredInputDeviceId(): string {
  return readString(STORAGE_KEY_INPUT);
}

/** Return the cached output device id (empty string = "default"). */
export function getStoredOutputDeviceId(): string {
  return readString(STORAGE_KEY_OUTPUT);
}

/** Persist a chosen input device. Pass `""` to reset to the default. */
export function setStoredInputDeviceId(deviceId: string): void {
  writeString(STORAGE_KEY_INPUT, deviceId);
}

/** Persist a chosen output device. Pass `""` to reset to the default. */
export function setStoredOutputDeviceId(deviceId: string): void {
  writeString(STORAGE_KEY_OUTPUT, deviceId);
}

export interface DspPreferences {
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
}

export function getStoredDspPreferences(): DspPreferences {
  return {
    echoCancellation: readBoolean(STORAGE_KEY_DSP_EC, true),
    noiseSuppression: readBoolean(STORAGE_KEY_DSP_NS, true),
    autoGainControl: readBoolean(STORAGE_KEY_DSP_AGC, true),
  };
}

export function setStoredDspPreferences(prefs: Partial<DspPreferences>): void {
  if (typeof prefs.echoCancellation === "boolean") {
    writeBoolean(STORAGE_KEY_DSP_EC, prefs.echoCancellation);
  }
  if (typeof prefs.noiseSuppression === "boolean") {
    writeBoolean(STORAGE_KEY_DSP_NS, prefs.noiseSuppression);
  }
  if (typeof prefs.autoGainControl === "boolean") {
    writeBoolean(STORAGE_KEY_DSP_AGC, prefs.autoGainControl);
  }
}

/** Probe the browser's permission API for microphone access. */
export async function queryMicPermission(): Promise<MicPermissionState> {
  if (typeof navigator === "undefined" || !("permissions" in navigator)) {
    return "unknown";
  }
  try {
    // ``microphone`` is in the PermissionsAPI spec but only Chromium
    // implements it widely; on Firefox/Safari this throws.
    const status = await navigator.permissions.query({
      name: "microphone" as PermissionName,
    });
    return status.state as MicPermissionState;
  } catch {
    return "unknown";
  }
}

/**
 * Trigger the browser's microphone permission prompt and immediately
 * release the stream. Returns `true` if the user granted access.
 */
export async function requestMicPermission(): Promise<boolean> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices) {
    return false;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: true,
      video: false,
    });
    for (const track of stream.getTracks()) track.stop();
    return true;
  } catch {
    return false;
  }
}

/** Enumerate the available audio devices. */
export async function listDevices(): Promise<DeviceLists> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices) {
    return { inputs: [], outputs: [] };
  }
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs: DeviceListing[] = [];
    const outputs: DeviceListing[] = [];
    for (const dev of devices) {
      const listing: DeviceListing = {
        deviceId: dev.deviceId,
        label: dev.label,
        groupId: dev.groupId,
      };
      if (dev.kind === "audioinput") inputs.push(listing);
      else if (dev.kind === "audiooutput") outputs.push(listing);
    }
    return { inputs, outputs };
  } catch {
    return { inputs: [], outputs: [] };
  }
}

/** Subscribe to `devicechange` and invoke the callback with a fresh listing. */
export function onDeviceListChange(handler: () => void): () => void {
  if (typeof navigator === "undefined" || !navigator.mediaDevices) {
    return () => {
      /* noop */
    };
  }
  const listener = (): void => {
    try {
      handler();
    } catch {
      /* ignore */
    }
  };
  navigator.mediaDevices.addEventListener("devicechange", listener);
  return () => {
    navigator.mediaDevices.removeEventListener("devicechange", listener);
  };
}
