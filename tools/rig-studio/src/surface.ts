import type { Parameter, Rig, Values } from './model.js'

/** Atlas UVs use top-left origin. Positions are interleaved local x,y artwork pixels. */
export interface ShapeKey { at: number; offsets?: number[]; opacity?: number; order?: number }
export interface SurfacePose {
  xy: number[]
  opacity: number
  order: number
  shapes: Record<string, ShapeKey[]>
}
export interface JointWeights { bone:string; weights:number[] }
export interface Surface {
  /** Unassigned weight stays on the layer joint. A single influence is also accepted. */
  skin?: JointWeights | JointWeights[]
  uv: number[]
  indices: number[]
  masks: string[]
  inverted?: boolean
  blend: 'normal' | 'multiply' | 'add'
  base: SurfacePose
  variants?: Record<string, SurfacePose>
}
export interface SurfaceFrame { xy: Float32Array; opacity: number; order: number }

export function validateSurfaces(rig: Rig) {
  if (!rig.layers.some(l => l.surface)) return
  const layerIds = new Set(rig.layers.map(l => l.id))
  const params = new Map(rig.parameters.map(p => [p.id,p]))
  const variant = rig.variantParameter ? params.get(rig.variantParameter) : undefined
  if (rig.variantParameter && !variant?.options?.length) throw Error('A variant parameter needs named options')
  for (const layer of rig.layers) {
    const s = layer.surface
    function fail(reason: string): never { throw Error(layer.id + ': ' + reason) }
    if (!s || layer.mesh) throw Error('Atlas rigs require a surface on each layer')
    if (!['normal','add','multiply'].includes(s.blend) || !s.masks.every(id => layerIds.has(id))) fail('invalid blend or mask')
    const size = s.uv.length
    if (size < 6 || size % 2 || !s.uv.every(Number.isFinite) || size > 131072) fail('invalid UV pairs')
    if (s.skin) {
      const skins = Array.isArray(s.skin) ? s.skin : [s.skin]
      const total = new Float64Array(size/2)
      for (const skin of skins) {
        if (!rig.bones.some(b => b.id === skin.bone) || skin.weights.length !== total.length) fail('invalid skin joint')
        for (const [i,w] of skin.weights.entries()) {
          total[i] += w
          if (!Number.isFinite(w) || w < 0 || w > 1 || total[i] > 1.000001) fail('invalid joint weights')
        }
      }
    }
    if (!s.indices.length || s.indices.length % 3 || !s.indices.every(i =>
      Number.isInteger(i) && i >= 0 && i < size/2)) fail('invalid triangle indices')
    for (const name of Object.keys(s.variants ?? {})) {
      if (!variant || !variant.options?.some(option => String(option.value) === name)) fail('unknown variant')
    }
    for (const pose of [s.base, ...Object.values(s.variants ?? {})]) {
      if (pose.xy.length !== size || !pose.xy.every(Number.isFinite)
        || !Number.isFinite(pose.opacity) || !Number.isFinite(pose.order)) fail('invalid bind pose')
      for (const [id, keys] of Object.entries(pose.shapes)) {
        const p = params.get(id)
        if (!p || !keys.length || !keys.some(k => k.at === p.default)) fail('missing neutral shape for ' + id)
        if (!keys.every((k,i) => Number.isFinite(k.at) && (i === 0 || keys[i-1].at < k.at)
          && (!k.offsets || (k.offsets.length === size && k.offsets.every(Number.isFinite)))
          && Number.isFinite(k.opacity ?? 0) && Number.isFinite(k.order ?? 0))) fail('invalid shape for ' + id)
      }
    }
  }
}

export function optionValue(p: Parameter, value: number) {
  return p.options?.reduce((best, next) => Math.abs(next.value-value) < Math.abs(best.value-value) ? next : best).value ?? value
}

export function deform(surface: Surface, values: Values, variant?: number): SurfaceFrame {
  const base = surface.variants?.[String(variant)] ?? surface.base
  const xy = Float32Array.from(base.xy)
  let opacity = base.opacity, order = base.order
  function apply(key: ShapeKey, weight: number) {
    if (!weight) return
    opacity += (key.opacity ?? 0)*weight
    order += (key.order ?? 0)*weight
    if (key.offsets) for (let i = 0; i < xy.length; i++) xy[i] += key.offsets[i]*weight
  }
  for (const [id, keys] of Object.entries(base.shapes)) {
    const value = values[id]
    const end = keys.findIndex(key => key.at >= value)
    if (end < 0) apply(keys[keys.length-1], 1)
    else if (!end || keys[end].at === value) apply(keys[end], 1)
    else {
      const a = keys[end-1], b = keys[end], mix = (value-a.at)/(b.at-a.at)
      apply(a, 1-mix)
      apply(b, mix)
    }
  }
  return {xy, opacity: Math.min(1,Math.max(0,opacity)), order}
}
