import assert from 'node:assert/strict'
import {sample} from '../dist/runtime.js'
import {deform} from '../dist/surface.js'

// Feed the raw expression parameters independently of the public adapter.
// Alpha matters even for hidden pieces: a purple highlight can leak through spirals.
export function checkOverlays(model, rig) {
  const {parameters:p,drawables:d} = model
  const channels = [
    ['symbol.question','Param43'], ['symbol.sweat','Param44'], ['face.shadow','Param57'],
    ['face.blush','Param58'], ['face.tears','Param59'], ['prop.lollipop','Param60'],
    ['glasses.head','Param11'], ['glasses.face','Param64'],
    ['eyes.left.purple','Param62'], ['eyes.right.purple','Param63']
  ]
  const layers = rig.layers.map(layer => [layer,d.ids.findIndex(id => id.toLowerCase() === layer.id)])
  for (const [id,raw] of channels) {
    assert.ok(rig.parameters.some(param => param.id === id),id)
    assert.ok(p.ids.includes(raw),raw)
  }
  function compare(outfit, style, overrides) {
    p.values.set(p.defaultValues)
    const raw = {Param16:outfit ? 30 : 0, Param17:outfit === 2 ? 30 : 0,
      Param55:style === 1 ? 30 : 0, Param56:style === 2 ? 30 : 0}
    for (const [id,param] of channels) raw[param] = (overrides[id] ?? 0)*30
    for (const [id,value] of Object.entries(raw)) p.values[p.ids.indexOf(id)] = value
    model.update()
    const values = sample(rig,undefined,0,{'outfit.style':outfit,'eyes.style':style,...overrides})
    for (const [layer,index] of layers) {
      const frame = deform(layer.surface,values,outfit)
      assert.ok(Math.abs(frame.opacity-d.opacities[index]) < .002,
        layer.id+' opacity at '+JSON.stringify({outfit,style,overrides}))
    }
  }
  for (const outfit of [0,1,2]) {
    for (const [id] of channels) for (const value of [.25,.75,1]) compare(outfit,0,{[id]:value})
    for (const style of [0,1,2]) compare(outfit,style,Object.fromEntries(
      channels.map(([id],i) => [id,i%2 ? .85 : .45])))
  }
}
