// Offline authoring tool. Only the installed Core is evaluated here. The output plays without it.
import fs from 'node:fs/promises'
import path from 'node:path'
import vm from 'node:vm'
import {setTimeout as delay} from 'node:timers/promises'
import {calibration, clips, wardrobe} from './dist/alexia.js'
import {articulateAlexia} from './dist/anatomy.js'
import {measureInfluences} from './audit-atlas.mjs'

const [modelFile, outputFile] = process.argv.slice(2)
if (!modelFile || !outputFile) throw Error('Expected model.model3.json and a temporary output JSON path')
const root = path.dirname(modelFile)
const read = async name => JSON.parse(await fs.readFile(name,'utf8'))
const {FileReferences: files} = await read(modelFile)
const corePath = new URL('../../web/public/live2d/live2dcubismcore.min.js',import.meta.url)
const sandbox = {atob,console,setTimeout,clearTimeout}
vm.runInNewContext(await fs.readFile(corePath,'utf8'),sandbox)
const core = sandbox.Live2DCubismCore
for (let attempt = 0; ; attempt++) {
  try { core.Version.csmGetVersion(); break }
  catch (error) { if (attempt >= 100) throw error; await delay(25) }
}
const bytes = await fs.readFile(path.resolve(root,files.Moc))
const moc = core.Moc.fromArrayBuffer(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength))
if (!moc) throw Error('Installed Core could not read this model')
const model = core.Model.fromMoc(moc)
const d = model.drawables, p = model.parameters
const info = files.DisplayInfo ? await read(path.resolve(root,files.DisplayInfo)) : {}
const names = new Map((info.Parts ?? []).map(e => [e.Id,e.Name]))
const profile = calibration.filter(c => Object.keys(c.source).every(id => p.ids.includes(id)))
if (!profile.some(c => c.id === wardrobe)) throw Error('This calibration expects the Alexia wardrobe channels')
const width = model.canvasinfo.CanvasWidth, height = model.canvasinfo.CanvasHeight
const scale = 2048 / Math.max(width,height), unit = model.canvasinfo.PixelsPerUnit * scale
const round = n => Math.round(n*1000)/1000
const rig = {version:1,name:'Aiko',width:width*scale,height:height*scale,variantParameter:wardrobe,
  bones:[{id:'root',x:0,y:0}],parameters:profile.map(({source,knots,...parameter}) => parameter),
  layers:[],clips:clips.filter(c => Object.keys(c.tracks).every(id => profile.some(p => p.id === id)))}
const inventory = []
const partPath = index => {
  const result = []
  for (let i = index; i >= 0; i = model.parts.parentIndices[i]) {
    const id = model.parts.ids[i]
    result.unshift(names.get(id) ?? id)
  }
  return result.join(' / ')
}
for (let i=0; i<d.count; i++) {
  const id = d.ids[i].toLowerCase(), flags = d.constantFlags[i]
  const group = partPath(d.parentPartIndices[i])
  rig.layers.push({id,label:group + ' · ' + d.ids[i],bone:'root',src:id+'.png',x:0,y:0,surface:{
    uv:Array.from(d.vertexUvs[i],(n,j) => j%2 ? 1-n : n),indices:Array.from(d.indices[i]),
    masks:Array.from(d.masks[i].slice(0,d.maskCounts[i]),j => d.ids[j].toLowerCase()),
    inverted:core.Utils.hasIsInvertedMaskBit(flags),
    blend:core.Utils.hasBlendMultiplicativeBit(flags) ? 'multiply' : core.Utils.hasBlendAdditiveBit(flags) ? 'add' : 'normal'
  }})
  inventory.push({id,sourceId:d.ids[i],group,atlas:d.textureIndices[i]})
}

function set(control, knot) {
  for (const [id, table] of Object.entries(control.source)) p.values[p.ids.indexOf(id)] = table[knot]
}
function snapshot() {
  model.update()
  return rig.layers.map((_,i) => ({
    xy:Array.from(d.vertexPositions[i],(n,j) => round(j%2 ? rig.height/2-n*unit : rig.width/2+n*unit)),
    opacity:round(d.opacities[i]),order:d.renderOrders[i],shapes:{}
  }))
}
const outfit = profile.find(c => c.id === wardrobe)
if (process.argv[4] === '--check-motion') {
  const {checkMotion} = await import('./test/check-motion.mjs')
  const {checkOverlays} = await import('./test/check-overlays.mjs')
  const exported = await read(outputFile)
  checkMotion(model,exported,profile)
  checkOverlays(model,exported)
  model.release()
  moc._release()
  process.exit(0)
}
if (process.argv[4] === '--audit') {
  const labels = new Map((info.Parameters ?? []).map(row => [row.Id,row.Name]))
  const report = measureInfluences(model,inventory,labels,outfit)
  await fs.writeFile(outputFile,JSON.stringify(report,null,2),{flag:'wx'})
  model.release()
  moc._release()
  process.exit(0)
}
for (let variant=0; variant<outfit.knots.length; variant++) {
  p.values.set(p.defaultValues)
  set(outfit,variant)
  const poses = snapshot()
  for (const control of profile.filter(c => c !== outfit)) {
    const frames = control.knots.map((at,j) => {
      if (at === control.default) return poses
      set(control,j)
      return snapshot()
    })
    set(control,control.knots.indexOf(control.default))
    for (let i=0; i<d.count; i++) {
      const base = poses[i]
      const keys = frames.map((frame,j) => {
        const actual = frame[i], key = {at:control.knots[j]}
        const offsets = actual.xy.map((value,k) => round(value-base.xy[k]))
        if (offsets.some(v => Math.abs(v) > .002)) key.offsets = offsets
        if (actual.opacity !== base.opacity) key.opacity = round(actual.opacity-base.opacity)
        if (actual.order !== base.order) key.order = actual.order-base.order
        return key
      })
      if (keys.some(key => Object.keys(key).length > 1)) base.shapes[control.id] = keys
    }
  }
  for (const [i, pose] of poses.entries()) {
    const s = rig.layers[i].surface
    if (!variant) s.base = pose
    else if (JSON.stringify(s.base) !== JSON.stringify(pose)) (s.variants ??= {})[variant] = pose
  }
}
const geometry = new Set(rig.layers.filter(l => l.surface.indices.length >= 3).map(l => l.id))
rig.layers = rig.layers.filter(l => geometry.has(l.id))
for (const layer of rig.layers) {
  if (layer.surface.masks.some(id => !geometry.has(id))) throw Error('Empty clipping source on ' + layer.id)
}
const kept = inventory.filter(l => geometry.has(l.id))
articulateAlexia(rig,kept)
await fs.writeFile(outputFile,JSON.stringify({rig,inventory:kept,textures:files.Textures}),{flag:'wx'})
model.release()
moc._release()
