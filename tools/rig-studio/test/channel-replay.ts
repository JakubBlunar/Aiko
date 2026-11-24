// Browser entry for channel_replay.py. Uses the app channels and the local wardrobe rig.
import { AvatarEngine } from "../../../web/src/live2d/AvatarEngine"
import { RigStudioAdapter } from "../../../web/src/live2d/RigStudioAdapter"
import { ExpressionChannel } from "../../../web/src/live2d/channels/ExpressionChannel"
import { OverlayChannel } from "../../../web/src/live2d/channels/OverlayChannel"
import { LipsyncChannel } from "../../../web/src/live2d/channels/LipsyncChannel"
import { GestureChannel } from "../../../web/src/live2d/channels/GestureChannel"
import { MotionChannel } from "../../../web/src/live2d/channels/MotionChannel"
import { OutfitChannel } from "../../../web/src/live2d/channels/OutfitChannel"
import { AccessoryChannel } from "../../../web/src/live2d/channels/AccessoryChannel"
import { AmbientBodyChannel } from "../../../web/src/live2d/channels/AmbientBodyChannel"
import { GazeChannel } from "../../../web/src/live2d/channels/GazeChannel"
import { ReachChannel } from "../../../web/src/live2d/channels/ReachChannel"
import { IdleLifeChannel } from "../../../web/src/live2d/channels/IdleLifeChannel"
import { SleepChannel } from "../../../web/src/live2d/channels/SleepChannel"
import { createEngineState } from "../../../web/src/live2d/state"
import type { AvatarManifest } from "../../../web/src/live2d/types"
import { ManualRaf } from "../../../web/src/live2d/__fixtures__/manual-raf"
import { buildStoreSnapshot } from "../../../web/src/live2d/__fixtures__/test-manifest"
import { gestureLayer, idleLayers, loadImages, sampleLayers, validateRig } from "../src/runtime"
import { createRigRenderer } from "../src/player"

export async function start(manifest: AvatarManifest, capture: boolean) {
  const rig = validateRig(await (await fetch("/local/rig.json")).json())
  const images = await loadImages(rig,src => "/local/"+src)
  const canvas = document.createElement("canvas")
  canvas.width = 360
  canvas.height = 600
  const renderer = createRigRenderer(canvas,rig,images)
  const state = createEngineState(), raf = new ManualRaf(), snapshot = buildStoreSnapshot()
  let now = 1000
  const adapter = new RigStudioAdapter(rig,manifest,state,() => now)
  // Compare the installed expression names against the public pose controls
  // before any app channel starts driving partial strength or speech.
  const poses: Record<string,[string,number]> = {
    bbt:["prop.lollipop",1],dyj:["glasses.face",1],h:["symbol.sweat",1],k:["face.tears",1],
    lh:["face.blush",1],lzx:["face.grin",1],mj:["glasses.head",1],sq:["face.shadow",1],
    wh:["symbol.question",1],xxy:["eyes.style",1],y:["eyes.style",2],yf:["outfit.style",1],
    yfmz:["outfit.style",2],yjys1:["eyes.left.purple",1],yjys2:["eyes.right.purple",1],zs1:["arms.crossed",1]
  }
  for (const {name} of manifest.expressions) {
    const [id,value] = poses[name]
    adapter.expression(name)
    if (adapter.sampleFrame([])[id] !== value) throw Error("Expression mismatch: "+name)
  }
  adapter.resetExpression()
  const engine = new AvatarEngine({manifest,engineState:state,getStoreSnapshot:() => snapshot,
    now:() => now,scheduleFrame:raf.schedule,cancelFrame:raf.cancel})
  engine.register(new MotionChannel(),new OutfitChannel(),new AccessoryChannel(),new OverlayChannel(),
    new LipsyncChannel(),new ExpressionChannel(),new GestureChannel(),new GazeChannel(),
    new AmbientBodyChannel(),new ReachChannel(),new IdleLifeChannel(),new SleepChannel())
  engine.start(adapter)
  const wrist = rig.clips.find(c => c.id === "wrist")!, tail = rig.clips.find(c => c.id === "tail")!
  return {
    frame(i: number) {
      const t = i/30
      now = 1000+t*1000
      if (i === 0) engine.dispatchReaction("cheerful")
      if (i === 6) engine.dispatchMotion({group:"Tap",index:manifest.motions.Tap.findIndex(m => m.name === "nod"),
        name:"nod",firedAt:1})
      if (i === 15) engine.dispatchOverlay({name:"tail_wag",expiresAt:Date.now()+600})
      if (i === 40) snapshot.audioAmplitude = .7
      if (i === 76) { snapshot.audioAmplitude = 0; engine.dispatchReaction("sad") }
      if (i === 110) engine.dispatchOverlay({name:"stars",expiresAt:Date.now()+800})
      if (i === 140 || i === 190) {
        const name = i === 140 ? "bow" : "shake"
        engine.dispatchMotion({group:"Tap",index:manifest.motions.Tap.findIndex(m => m.name === name),
          name,firedAt:i})
      }
      if (i === 172) engine.dispatchReaction("")
      raf.flush(2)
      const layers = [...idleLayers(rig,t),gestureLayer(wrist,t-.4)]
      const values = adapter.sampleFrame(layers)
      const expected = sampleLayers(rig,[...layers,gestureLayer(tail,t-.5)])
      for (const id of ["tail.curl","hand.left.bend","finger.left.index","finger.right.index"]) {
        if (Math.abs(values[id]-expected[id]) > 1e-8) throw Error(id+" diverged at frame "+i)
      }
      renderer.render(values)
      return {values,png:(i === 0 || i === 112 || (capture && i%2 === 0)) ? canvas.toDataURL().split(",")[1] : undefined}
    },
    stop() { engine.stop(); renderer.dispose() }
  }
}
