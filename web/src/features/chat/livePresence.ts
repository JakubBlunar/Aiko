import { api } from "@/api";
import { queryMicPermission } from "@/audio/DeviceManager";
import type { AssistantSettings } from "@/types";

export async function liveMicNeedsConsent(consented: boolean): Promise<boolean> {
  if (consented) return false;
  const perm = await queryMicPermission();
  return perm !== "granted";
}

export async function enableLivePresence(opts: {
  consented?: boolean;
  visualOnly?: boolean;
}): Promise<AssistantSettings> {
  const companion: Record<string, unknown> = {
    behavior_posture: "live_presence",
  };
  if (opts.visualOnly) {
    companion.live_unprompted_speech = false;
  }
  if (opts.consented) {
    companion.live_mic_consented = true;
  }
  return api.patchSettings({ companion });
}

export async function disableLivePresence(): Promise<AssistantSettings> {
  return api.patchSettings({
    companion: { behavior_posture: "turn_based" },
  });
}
