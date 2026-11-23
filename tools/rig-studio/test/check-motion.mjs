import assert from 'node:assert/strict'
import {deform} from '../dist/surface.js'
import {sample,validateRig} from '../dist/runtime.js'

// Opt-in integration check against the locally installed mesh. Evaluate between
// baked keys: comparing only the captured endpoints misses a collapsing tail.
export function checkMotion(model, rig, profile) {
  validateRig(rig)
  const {parameters:p,drawables:d,canvasinfo:canvas} = model
  const px = rig.height*canvas.PixelsPerUnit/canvas.CanvasHeight
  const peak = {tail:0,grin:0,grinOpacity:0}
  function write(raw,value) { p.values[p.ids.indexOf(raw)] = value }
  for (const outfit of [0,1,2]) {
    function reset() {
      p.values.set(p.defaultValues)
      write('Param16',outfit ? 30 : 0)
      write('Param17',outfit === 2 ? 30 : 0)
    }
    function compare(id, overrides, kind) {
      const values = sample(rig,undefined,0,{'outfit.style':outfit,...overrides})
      const index = d.ids.indexOf(id)
      const layer = rig.layers.find(l => l.id === id.toLowerCase())
      const frame = deform(layer.surface,values,outfit)
      const coords = d.vertexPositions[index]
      for (let k=0; k<coords.length; k+=2) {
        peak[kind] = Math.max(peak[kind],Math.hypot(
          frame.xy[k]-(rig.width/2+px*coords[k]),frame.xy[k+1]-(rig.height/2-px*coords[k+1])))
      }
      if (kind === 'grin') peak.grinOpacity = Math.max(peak.grinOpacity,Math.abs(frame.opacity-d.opacities[index]))
    }
    reset()
    const tail = profile.find(c => c.id === 'tail.curl')
    const loop = rig.clips.find(clip => clip.id === 'tail')
    const start = sample(rig,loop,0,{'outfit.style':outfit})
    const end = sample(rig,loop,loop.duration,{'outfit.style':outfit})
    for (const layer of rig.layers) {
      assert.deepEqual(deform(layer.surface,start,outfit),deform(layer.surface,end,outfit),
        'Tail loop seam on ' + layer.id)
    }
    for (let j=1; j<tail.knots.length; j++) {
      const phase = (tail.knots[j-1]+tail.knots[j])/2
      Object.entries(tail.source).forEach(([raw,values]) => write(raw,(values[j-1]+values[j])/2))
      model.update()
      for (const id of ['ArtMesh231','ArtMesh232','ArtMesh233','ArtMesh234']) compare(id,{'tail.curl':phase},'tail')
    }
    for (const grin of [.125,.375,.625,.875,1]) {
      reset()
      write('Param54',30*grin)
      write('Param50',.7)
      model.update()
      for (const id of ['ArtMesh18','ArtMesh115','ArtMesh101']) compare(id,{'face.grin':grin,'jaw.drop':.7},'grin')
    }
  }
  console.log('Worst between-key error at 2048 px:',peak)
  assert.ok(peak.tail < 1 && peak.grin < 1 && peak.grinOpacity < .002)
}
