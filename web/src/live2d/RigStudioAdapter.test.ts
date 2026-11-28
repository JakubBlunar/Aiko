import { expect, it } from "vitest";
import { AvatarEngine } from "./AvatarEngine";
import { RigStudioAdapter } from "./RigStudioAdapter";
import { createEngineState } from "./state";
import { ExpressionChannel } from "./channels/ExpressionChannel";
import { OverlayChannel } from "./channels/OverlayChannel";
import { MotionChannel } from "./channels/MotionChannel";
import { GestureChannel } from "./channels/GestureChannel";
import { LipsyncChannel } from "./channels/LipsyncChannel";
import { ManualRaf } from "./__fixtures__/manual-raf";
import { buildManifest, buildStoreSnapshot } from "./__fixtures__/test-manifest";
import { calibration, clips, restingClips, restingMotion } from "../../../tools/rig-studio/src/alexia";
import type { Rig } from "../../../tools/rig-studio/src/model";
import { gestureLayer, idleLayers, sampleLayers } from "../../../tools/rig-studio/src/runtime";

const finger = "finger.right.index";
const rig: Rig = {
  version:1, name:"Channel test", width:100, height:100, bones:[], layers:[],
  parameters:[...calibration,{id:finger,min:0,max:1,default:0}],
  clips:[...clips,...restingClips,{id:"fingers",duration:3,tracks:{[finger]:[[0,0],[1,1],[3,0]]}}],
  idleMotion:restingMotion,
};
const manifest = buildManifest({
  reaction_mapping:{cheerful:"lzx",sad:"k"},
  expression_params:{
    lzx:[{param_id:"Param54",on_value:30}], k:[{param_id:"Param59",on_value:30}],
    star:[{param_id:"Param55",on_value:30}],
  },
  mouth_overlay_param_ids:["Param54"],
  capabilities:{has_tail_wag:true},
  overlays:{
    stars:{param_id:"expr:star",on_value:30,decay_ms:200,label_en:"Stars"},
    blush:{param_id:"Param58",on_value:30,decay_ms:200,label_en:"Blush"},
  },
  motions:{Tap:[{name:"nod",file:"nod.motion3.json"}]},
});

it("plays app reactions, motion and both overlay kinds over a native tail turn and idle", () => {
  const raf = new ManualRaf();
  let now = 1000;
  const snapshot = buildStoreSnapshot();
  const engineState = createEngineState();
  const adapter = new RigStudioAdapter(rig,manifest,engineState,() => now);
  const engine = new AvatarEngine({manifest,engineState,now:() => now,
    getStoreSnapshot:() => snapshot,scheduleFrame:raf.schedule,cancelFrame:raf.cancel});
  engine.register(new MotionChannel(),new ExpressionChannel(),new GestureChannel(),new OverlayChannel(),new LipsyncChannel());
  engine.start(adapter);
  const turn = clips.find(c => c.id === "tail")!;
  function tick() {
    now += 1000/60;
    raf.flush(2);
    const seconds = (now-1000)/1000;
    const layers = idleLayers(rig,seconds);
    const base = sampleLayers(rig,[...layers,gestureLayer(turn,seconds-1/60)]);
    const pose = adapter.sampleFrame(layers);
    for (const id of ["body.breath","tail.curl",finger]) expect(pose[id]).toBeCloseTo(base[id],10);
    return pose;
  }
  try {
    engine.dispatchReaction("cheerful");
    engine.dispatchOverlay({name:"tail_wag",expiresAt:Date.now()+400});
    engine.dispatchMotion({group:"Tap",index:0,name:"nod",firedAt:1});
    let pose = tick();
    for (let i=1;i<18;i++) pose = tick();
    expect(pose["head.pitch"]).toBeCloseTo(-15);
    expect(pose["face.grin"]).toBe(1);
    snapshot.audioAmplitude = .8;
    for (let i=0;i<90;i++) pose = tick();
    expect(pose["mouth.open"]).toBeCloseTo(.8);
    expect(pose["face.grin"]).toBe(0);
    expect(pose["head.pitch"]).toBe(0);
    engine.dispatchReaction("sad");
    for (let i=0;i<30;i++) pose = tick();
    expect(pose["face.tears"]).toBeGreaterThan(.4);
    for (const name of ["stars","blush"]) engine.dispatchOverlay({name,expiresAt:Date.now()+200});
    pose = tick();
    expect(pose["eyes.style"]).toBe(1);
    expect(pose["face.tears"]).toBe(0);
    expect(pose["face.blush"]).toBe(1);
    for (let i=0;i<40;i++) pose = tick();
    expect(pose["face.tears"]).toBeGreaterThan(.4);
    expect(pose["eyes.style"]).toBe(0);
    expect(pose["face.blush"]).toBe(0);
    // An expired pulse must give a native blush track back to its author.
    const blush = {id:"native",duration:1,tracks:{"face.blush":[[0,.3] as [number, number]]}};
    expect(adapter.sampleFrame([{clip:blush,seconds:0}])["face.blush"]).toBe(.3);
    engine.dispatchReaction("");
    expect(tick()["face.tears"]).toBe(0);
    for (let i=0;i<250;i++) tick(); // Across the turn's release and back into idle.
  } finally { engine.stop(); }
});

it("combines outfit writes before gating arms and leaves unowned outfit layers free", () => {
  const adapter = new RigStudioAdapter(rig,manifest,createEngineState());
  adapter.setParam("Param61",30);
  adapter.setParam("Param17",30);
  adapter.setParam("Param16",30);
  expect(adapter.sampleFrame([])).toMatchObject({"outfit.style":2,"arms.crossed":0});
  adapter.setParam("Param17",0);
  expect(adapter.sampleFrame([])["outfit.style"]).toBe(1);
  adapter.setParam("Param16",0);
  expect(adapter.sampleFrame([])).toMatchObject({"outfit.style":0,"arms.crossed":1});
});

it("plays the chat flick on the editable phase and releases to the moving Studio idle", () => {
  let now = 1000;
  const raf = new ManualRaf();
  const engineState = createEngineState();
  const adapter = new RigStudioAdapter(rig, manifest, engineState, () => now);
  const engine = new AvatarEngine({manifest, engineState, now:() => now,
    getStoreSnapshot:buildStoreSnapshot, scheduleFrame:raf.schedule, cancelFrame:raf.cancel});
  engine.register(new GestureChannel());
  engine.start(adapter);
  try {
    const sampleTail = () => adapter.sampleFrame()["tail.curl"];
    const entry = sampleTail();
    engine.dispatchOverlay({name:"tail_flick", expiresAt:Date.now()+2000});
    expect(sampleTail()).toBeCloseTo(entry);
    now += 750;
    expect(sampleTail()).toBeCloseTo(-.42);
    now += 450;
    expect(sampleTail()).toBeCloseTo(-.42);
    now += 810;
    const idle = sampleLayers(rig,idleLayers(rig,now/1000));
    expect(sampleTail()).toBeCloseTo(idle["tail.curl"]);
  } finally { engine.stop(); }
});
