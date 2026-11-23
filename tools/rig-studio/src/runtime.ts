// Pure, absolute-time evaluation. All positions are pixels in the neutral artwork.
import type { Bone, BonePose, Clip, Key, Matrix, Parameter, Rig, Values } from './model.js'
import { drawMesh, skin } from './mesh.js'
import { optionValue, validateSurfaces } from './surface.js'
const finite = Number.isFinite
const channels = new Set(['x', 'y', 'rotation', 'scaleX', 'scaleY', 'opacity'])
const check = (ok: unknown, msg: string) => { if (!ok) throw new Error(msg) }
export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

function unique(rows: { id: string }[], kind: string) {
  check(Array.isArray(rows), 'Expected an array of ' + kind)
  const ids = new Set()
  for (const row of rows) {
    check(typeof row.id === 'string' && /^[a-z][a-z0-9_.-]*$/.test(row.id)
      && !ids.has(row.id), 'Invalid or duplicate ' + kind + ' ID: ' + row.id)
    ids.add(row.id)
  }
  return ids
}

function keysValid(keys: Key[]) {
  return Array.isArray(keys) && keys.length > 0 && keys.every((pair, i) =>
    Array.isArray(pair) && pair.length === 2 && pair.every(finite)
    && (!i || keys[i - 1][0] < pair[0]))
}

export function validateRig(rig: Rig): Rig {
  check(rig.version === 1, 'Expected rig format version 1')
  check(typeof rig.name === 'string' && rig.name.trim(), 'Missing rig name')
  check([rig.width, rig.height].every(v => finite(v) && v > 0), 'Invalid canvas size')
  const parameters = unique(rig.parameters, 'parameter')
  for (const p of rig.parameters) {
    if (p.enabledWhen) {
      const condition = p.enabledWhen
      const allowed = Array.isArray(condition.equals) ? condition.equals : [condition.equals]
      check(allowed.length && allowed.every(finite) && rig.parameters.some(other =>
        other.id === condition.id && !other.enabledWhen && other.id !== p.id),
      'Invalid parameter condition: ' + p.id)
    }
    if (p.options) check(p.options.length && p.options.every(o => finite(o.value) && o.value >= p.min
      && o.value <= p.max && typeof o.label === 'string') && p.options.some(o => o.value === p.default)
      && new Set(p.options.map(o => o.value)).size === p.options.length, 'Invalid options: ' + p.id)
    check([p.min, p.default, p.max].every(finite) && p.min < p.max
      && p.min <= p.default && p.default <= p.max, 'Invalid range: ' + p.id)
  }
  unique(rig.bones, 'bone')
  const bones = new Set()
  for (const b of rig.bones) {
    check((!b.parent || bones.has(b.parent)) && finite(b.x) && finite(b.y)
      && finite(b.scaleX ?? 1) && b.scaleX !== 0,
      'Check pivot and parent-first bone order: ' + b.id)
    for (const drive of b.drives ?? []) {
      check(parameters.has(drive.param) && channels.has(drive.channel) && keysValid(drive.keys),
        'Invalid driver on bone ' + b.id)
    }
    bones.add(b.id)
  }
  unique(rig.layers, 'layer')
  for (const l of rig.layers) {
    check(bones.has(l.bone) && finite(l.x) && finite(l.y) && typeof l.src === 'string'
      && /\.(png|webp|jpe?g|svg)$/i.test(l.src)
      && l.src.split('/').every(part => part && !['.','..'].includes(part) && !/[:\\?#%]/.test(part)),
      'Invalid image or placement: ' + l.id)
    if (l.mesh) {
      const {vertices, triangles} = l.mesh
      check(vertices.length >= 3 && triangles.length, 'Empty mesh: ' + l.id)
      for (const v of vertices) {
        const weights = Object.entries(v.weights)
        check(finite(v.x) && finite(v.y) && weights.every(([id, w]) => bones.has(id) && finite(w) && w >= 0)
          && Math.abs(weights.reduce((sum, [,w]) => sum+w, 0) - 1) < 0.00001, 'Invalid mesh weights: ' + l.id)
      }
      for (const tri of triangles) {
        check(tri.length === 3 && tri.every(i => Number.isInteger(i) && i >= 0 && i < vertices.length),
          'Invalid triangle indices: ' + l.id)
        const [a,b,c] = tri.map(i => vertices[i])
        check(Math.abs((b.x-a.x)*(c.y-a.y) - (b.y-a.y)*(c.x-a.x)) > 1e-8, 'Degenerate triangle: ' + l.id)
      }
    }
  }
  unique(rig.clips, 'clip')
  for (const clip of rig.clips) {
    check(finite(clip.duration) && clip.duration > 0, 'Invalid clip duration: ' + clip.id)
    for (const [id, keys] of Object.entries(clip.tracks)) {
      check(parameters.has(id) && keysValid(keys) && keys[0][0] >= 0
        && keys[keys.length - 1][0] <= clip.duration, 'Invalid track: ' + id)
    }
  }
  if (rig.layers.some(l => l.mesh)) {
    const neutral = pose(rig, sample(rig, undefined, 0))
    for (const l of rig.layers) if (l.mesh) skin(l, l.mesh, neutral, neutral)
  }
  validateSurfaces(rig)
  return rig
}

export function interpolate(keys: Key[], t: number): number {
  if (t <= keys[0][0]) return keys[0][1]
  for (let i = 1; i < keys.length; i++) {
    const [end, b] = keys[i]
    if (t <= end) {
      const [start, a] = keys[i - 1]
      return a + (b - a) * ((t - start) / (end - start))
    }
  }
  return keys[keys.length - 1][1]
}

export function parameterEnabled(condition: Parameter['enabledWhen'], values: Values) {
  if (!condition) return true
  const value = values[condition.id]
  return Array.isArray(condition.equals) ? condition.equals.includes(value) : condition.equals === value
}

export function sample(rig: Rig, clip: Clip | undefined, seconds: number, overrides: Values = {}): Values {
  const values: Values = Object.create(null)
  for (const p of rig.parameters) {
    const track = clip && Object.hasOwn(clip.tracks, p.id) ? clip.tracks[p.id] : undefined
    const value = Object.hasOwn(overrides, p.id) ? overrides[p.id] : track ? interpolate(track, seconds) : p.default
    values[p.id] = optionValue(p, clamp(value, p.min, p.max))
  }
  for (const p of rig.parameters) {
    if (!parameterEnabled(p.enabledWhen, values)) values[p.id] = p.default
  }
  return values
}

// Canvas affine matrices [a, b, c, d, tx, ty], composed parent first.
export function compose([a,b,c,d,x,y]: Matrix, [e,f,g,h,u,v]: Matrix): Matrix {
  return [a*e+c*f, b*e+d*f, a*g+c*h, b*g+d*h, x+a*u+c*v, y+b*u+d*v]
}

function localPose(bone: Bone, values: Values): BonePose {
  const t = { x: bone.x, y: bone.y, rotation: 0, scaleX: bone.scaleX ?? 1, scaleY: 1, opacity: 1 }
  for (const drive of bone.drives ?? []) {
    const value = interpolate(drive.keys, values[drive.param])
    if (['scaleX', 'scaleY', 'opacity'].includes(drive.channel)) t[drive.channel] *= value
    else t[drive.channel] += value
  }
  const radians = t.rotation * Math.PI / 180
  const c = Math.cos(radians), s = Math.sin(radians)
  return {matrix:[c*t.scaleX, s*t.scaleX, -s*t.scaleY, c*t.scaleY, t.x, t.y], opacity:clamp(t.opacity, 0, 1)}
}

export function pose(rig: Rig, values: Values): Map<string, BonePose> {
  const result = new Map<string, BonePose>()
  for (const bone of rig.bones) {
    const parent = bone.parent ? result.get(bone.parent) : undefined
    const {matrix: local, opacity} = localPose(bone, values)
    result.set(bone.id, {
      matrix: parent ? compose(parent.matrix, local) : local,
      opacity: (parent?.opacity ?? 1) * opacity
    })
  }
  return result
}

/** Reposition a joint in parent coordinates while keeping the neutral artwork registered. */
export function movePivot(rig: Rig, bone: Bone, x: number, y: number) {
  const [a,b,c,d] = localPose(bone, sample(rig, undefined, 0)).matrix
  const det = a*d - b*c
  check(Math.abs(det) > 1e-10, 'Set a nonzero neutral scale before editing this pivot')
  const dx = x - bone.x, dy = y - bone.y
  const u = (d*dx - c*dy) / det, v = (a*dy - b*dx) / det
  for (const item of [...rig.layers.filter(l => l.bone === bone.id), ...rig.bones.filter(b => b.parent === bone.id)]) {
    item.x -= u
    item.y -= v
  }
  bone.x = x
  bone.y = y
}

export async function loadImages(rig: Rig, resolve: (source: string) => string) {
  const images = new Map<string, HTMLImageElement>()
  await Promise.all([...new Set(rig.layers.map(l => l.src))].map(async src => {
    const image = new Image()
    image.src = resolve(src)
    try { await image.decode() } catch { throw new Error('Cannot decode layer ' + src) }
    images.set(src, image)
  }))
  return images
}

export function draw(ctx: CanvasRenderingContext2D, rig: Rig, images: Map<string, HTMLImageElement>,
  values: Values, skeleton = false) {
  const bones = pose(rig, values)
  const bind = rig.layers.some(l => l.mesh) ? pose(rig, sample(rig, undefined, 0)) : undefined
  const {width, height} = ctx.canvas
  ctx.clearRect(0, 0, width, height)
  const fit = Math.min(width / rig.width, height / rig.height)
  ctx.save()
  ctx.translate((width - rig.width * fit) / 2, (height - rig.height * fit) / 2)
  ctx.scale(fit, fit)
  for (const l of rig.layers) {
    const b = bones.get(l.bone)!
    if (b.opacity <= 0) continue
    ctx.save()
    ctx.globalAlpha = b.opacity
    if (l.mesh && bind) {
      drawMesh(ctx, images.get(l.src)!, l.mesh, skin(l, l.mesh, bind, bones), skeleton)
    } else {
      ctx.transform(...b.matrix)
      ctx.drawImage(images.get(l.src)!, l.x, l.y)
    }
    ctx.restore()
  }
  if (skeleton) {
    ctx.lineWidth = 1.5 / fit
    ctx.strokeStyle = '#ef6343'
    ctx.fillStyle = '#ffffff'
    for (const b of rig.bones) {
      const [, , , , x, y] = bones.get(b.id)!.matrix
      ctx.beginPath()
      if (b.parent) {
        const parent = bones.get(b.parent)!.matrix
        ctx.moveTo(parent[4], parent[5])
        ctx.lineTo(x, y)
        ctx.stroke()
      }
      ctx.beginPath()
      ctx.arc(x, y, 3 / fit, 0, Math.PI * 2)
      ctx.fill()
      ctx.stroke()
    }
  }
  ctx.restore()
}
