/**
 * Public surface of the Live2D engine. The React component imports
 * exclusively from here to keep the call sites inside
 * ``Live2DAvatar.tsx`` boring.
 */
export { AvatarEngine } from "./AvatarEngine";
export { PixiLive2DAdapter } from "./PixiLive2DAdapter";
export { StoreBridge } from "./StoreBridge";
export { createEngineState } from "./state";
export type { EngineState } from "./state";
export type { EngineDependencies, MouseSource } from "./AvatarEngine";
export type { BridgedState, BridgedStore } from "./StoreBridge";
export type {
  AvatarChannel,
  AvatarManifest,
  ChannelDeps,
  ChannelStoreSnapshot,
  Live2DModelAdapter,
  MouseSnapshot,
  ResolvedOverlayEvent,
} from "./types";
