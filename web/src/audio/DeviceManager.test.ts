import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getStoredDspPreferences,
  getStoredInputDeviceId,
  getStoredOutputDeviceId,
  listDevices,
  onDeviceListChange,
  queryMicPermission,
  requestMicPermission,
  setStoredDspPreferences,
  setStoredInputDeviceId,
  setStoredOutputDeviceId,
} from "./DeviceManager";

class FakeStorage {
  private _data = new Map<string, string>();
  getItem(key: string) {
    return this._data.has(key) ? (this._data.get(key) as string) : null;
  }
  setItem(key: string, value: string) {
    this._data.set(key, value);
  }
  removeItem(key: string) {
    this._data.delete(key);
  }
  clear() {
    this._data.clear();
  }
}

beforeEach(() => {
  const storage = new FakeStorage();
  (globalThis as unknown as { window: unknown }).window = {
    localStorage: storage,
    AudioContext: class {},
  };
  (globalThis as unknown as { localStorage: FakeStorage }).localStorage = storage;
});

afterEach(() => {
  delete (globalThis as unknown as { navigator?: unknown }).navigator;
  delete (globalThis as unknown as { window?: unknown }).window;
  delete (globalThis as unknown as { localStorage?: unknown }).localStorage;
});

describe("storage helpers", () => {
  it("round-trips the input device id through localStorage", () => {
    expect(getStoredInputDeviceId()).toBe("");
    setStoredInputDeviceId("mic-1");
    expect(getStoredInputDeviceId()).toBe("mic-1");
    // Setting empty resets the entry.
    setStoredInputDeviceId("");
    expect(getStoredInputDeviceId()).toBe("");
  });

  it("round-trips the output device id through localStorage", () => {
    expect(getStoredOutputDeviceId()).toBe("");
    setStoredOutputDeviceId("speaker-1");
    expect(getStoredOutputDeviceId()).toBe("speaker-1");
  });

  it("returns DSP defaults when nothing is stored", () => {
    expect(getStoredDspPreferences()).toEqual({
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    });
  });

  it("persists DSP overrides individually", () => {
    setStoredDspPreferences({ echoCancellation: false });
    expect(getStoredDspPreferences()).toEqual({
      echoCancellation: false,
      noiseSuppression: true,
      autoGainControl: true,
    });
    setStoredDspPreferences({ noiseSuppression: false, autoGainControl: false });
    expect(getStoredDspPreferences()).toEqual({
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    });
  });
});

describe("queryMicPermission", () => {
  it("returns unknown when the API is missing", async () => {
    (globalThis as unknown as { navigator: object }).navigator = {};
    expect(await queryMicPermission()).toBe("unknown");
  });

  it("falls back to unknown if permissions.query throws", async () => {
    (globalThis as unknown as { navigator: object }).navigator = {
      permissions: {
        query: () => {
          throw new Error("no microphone permission descriptor");
        },
      },
    };
    expect(await queryMicPermission()).toBe("unknown");
  });

  it("returns the reported state", async () => {
    (globalThis as unknown as { navigator: object }).navigator = {
      permissions: {
        query: async () => ({ state: "granted" }),
      },
    };
    expect(await queryMicPermission()).toBe("granted");
  });
});

describe("requestMicPermission", () => {
  it("returns true on a successful getUserMedia + stops tracks", async () => {
    const stopped: string[] = [];
    const tracks = [
      { stop: () => stopped.push("a") },
      { stop: () => stopped.push("b") },
    ];
    const getUserMedia = vi.fn(async () => ({
      getTracks: () => tracks,
    }));
    (globalThis as unknown as { navigator: object }).navigator = {
      mediaDevices: { getUserMedia },
    };
    expect(await requestMicPermission()).toBe(true);
    expect(stopped).toEqual(["a", "b"]);
  });

  it("returns false on rejection", async () => {
    (globalThis as unknown as { navigator: object }).navigator = {
      mediaDevices: {
        getUserMedia: vi.fn(async () => {
          throw new Error("denied");
        }),
      },
    };
    expect(await requestMicPermission()).toBe(false);
  });
});

describe("listDevices", () => {
  it("partitions audioinput / audiooutput entries", async () => {
    const enumerateDevices = vi.fn(async () => [
      { deviceId: "in1", label: "Mic 1", groupId: "g1", kind: "audioinput" },
      { deviceId: "out1", label: "Speakers", groupId: "g2", kind: "audiooutput" },
      { deviceId: "cam1", label: "Cam", groupId: "g3", kind: "videoinput" },
    ]);
    (globalThis as unknown as { navigator: object }).navigator = {
      mediaDevices: { enumerateDevices },
    };
    const lists = await listDevices();
    expect(lists.inputs.map((d) => d.deviceId)).toEqual(["in1"]);
    expect(lists.outputs.map((d) => d.deviceId)).toEqual(["out1"]);
  });

  it("returns empty lists if enumeration throws", async () => {
    (globalThis as unknown as { navigator: object }).navigator = {
      mediaDevices: {
        enumerateDevices: vi.fn(async () => {
          throw new Error("not supported");
        }),
      },
    };
    expect(await listDevices()).toEqual({ inputs: [], outputs: [] });
  });
});

describe("onDeviceListChange", () => {
  it("subscribes / unsubscribes a devicechange listener", () => {
    const listeners = new Map<string, () => void>();
    (globalThis as unknown as { navigator: object }).navigator = {
      mediaDevices: {
        addEventListener: (kind: string, fn: () => void) => {
          listeners.set(kind, fn);
        },
        removeEventListener: (kind: string, fn: () => void) => {
          if (listeners.get(kind) === fn) listeners.delete(kind);
        },
      },
    };
    let callCount = 0;
    const unsub = onDeviceListChange(() => {
      callCount += 1;
    });
    expect(listeners.has("devicechange")).toBe(true);
    listeners.get("devicechange")?.();
    expect(callCount).toBe(1);
    unsub();
    expect(listeners.has("devicechange")).toBe(false);
  });
});
