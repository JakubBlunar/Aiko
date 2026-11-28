// Bundled for the Studio browser check. Exercise the app's actual post-physics hook.
import { AvatarEngine } from '../../../web/src/live2d/AvatarEngine'
import { createEngineState } from '../../../web/src/live2d/state'
import { PixiLive2DAdapter } from '../../../web/src/live2d/PixiLive2DAdapter'
import { GestureChannel } from '../../../web/src/live2d/channels/GestureChannel'
import { AmbientBodyChannel } from '../../../web/src/live2d/channels/AmbientBodyChannel'
import { buildStoreSnapshot } from '../../../web/src/live2d/__fixtures__/test-manifest'
import { ManualRaf } from '../../../web/src/live2d/__fixtures__/manual-raf'

export async function check(manifest) {
  const model = await window.PIXI.live2d.Live2DModel.from('/reference/tail-test.model3.json',
    {autoUpdate:false, autoInteract:false, motionPreload:'NONE'})
  const internal = model.internalModel, ids = manifest.cat_tail_param_ids
  if (!internal.physics || ids.length !== 5) throw Error('Expected Alexia and her tail physics')
  const adapter = new PixiLive2DAdapter(model), raf = new ManualRaf()
  let now = 1000, before, after
  const read = () => ids.map(id => adapter.getParam(id))
  const snapshot = buildStoreSnapshot()
  const engine = new AvatarEngine({manifest, engineState:createEngineState(),
    now:() => now, getStoreSnapshot:() => snapshot,
    scheduleFrame:raf.schedule, cancelFrame:raf.cancel})
  adapter.onBeforeModelUpdate(() => { before = read() })
  engine.register(new GestureChannel(), new AmbientBodyChannel())
  engine.start(adapter)
  adapter.onBeforeModelUpdate(() => { after = read() })
  try {
    const rate = 60
    for (const [name, duration, cycles] of [
      ['tail_wag_slow',4,1], ['tail_wag_fast',2,3], ['tail_flick',2,0],
    ]) {
      const start = now, samples = []
      engine.dispatchOverlay({name,expiresAt:Date.now()+duration*1000})
      for (let frame=0; frame<=rate*duration+1; frame++) {
        now = start + frame*1000/rate
        raf.flush(2)
        internal.update(1000/rate,now)
        samples.push(after)
      }
      if (after.some((value,i) => value !== before[i])) throw Error(name+' did not release')
      if (cycles) {
        const peaks = samples.filter((v,i) => i>1 && i<rate*duration-2 &&
          v[0]>samples[i-1][0] && v[0]>=samples[i+1][0] && v[0]>2)
        if (peaks.length !== cycles) throw Error(name+' peaks: '+peaks.length)
      } else {
        const held = samples[45]
        if (Math.max(...held.map(Math.abs)) < 5 || samples.slice(45,73).some(
          pose => pose.some((value,i) => Math.abs(value-held[i]) > .001))) {
          throw Error('Physics displaced the teasing hold')
        }
      }
    }
  } finally { engine.stop(); model.destroy() }
}
