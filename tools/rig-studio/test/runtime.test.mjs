import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import {gestureLayer, idleLayers, movePivot, pose, sample, sampleLayers, validateRig} from '../dist/runtime.js'
import {skin} from '../dist/mesh.js'
import {clips as aikoClips, restingClips} from '../dist/alexia.js'

const rig = JSON.parse(readFileSync(new URL('../example/rig.json', import.meta.url), 'utf8'))
test('all example motion tracks and image paths resolve', () => {
  validateRig(rig)
  for (const l of rig.layers) assert.ok(readFileSync(new URL('../example/' + l.src, import.meta.url)).length)
})
test('scrub, play and reverse seek produce the same frame without accumulated rotations', () => {
  const idle = rig.clips[0]
  const first = pose(rig, sample(rig, idle, 1))
  pose(rig, sample(rig, idle, 3.8))
  assert.deepEqual(pose(rig, sample(rig, idle, 1)), first)
  assert.equal(sample(rig, idle, 1)['body.breath'], .5)
  assert.equal(sample(rig, idle, 1, {'body.breath':10})['body.breath'], 1)
})
test('parent rotation carries the hand and visibility follows the costume control', () => {
  const rest = pose(rig, sample(rig, undefined, 0))
  const active = pose(rig, sample(rig, undefined, 0, {'hand.left.wave':1, 'outfit.rose':1}))
  assert.notDeepEqual(rest.get('hand.left').matrix.slice(4), active.get('hand.left').matrix.slice(4))
  assert.equal(rest.get('vest').opacity, 0)
  assert.equal(active.get('vest').opacity, 1)
})
test('invalid parents, paths and mistyped curve names fail during import', () => {
  for (const corrupt of [
    r => { r.bones[0].parent = r.bones[1].id },
    r => { r.layers[0].src = '../secret.png' },
    r => { r.layers[0].mesh.vertices[0].weights = {missing: 1} },
    r => { r.layers[0].mesh.triangles[0] = [0,0,0] },
    r => { r.clips[0].tracks['typo'] = [[0,0]] },
    r => { r.idleMotion = {duration:4,layers:[{clip:'missing'}]} },
    r => { r.idleMotion = {duration:.5,layers:[{clip:r.clips[0].id}]} },
    r => { r.bones[1].drives[0].keys = [[1,0],[0,1]] }
  ]) {
    const bad = structuredClone(rig)
    corrupt(bad)
    assert.throws(() => validateRig(bad))
  }
})

test('independent clocks, sparse priority and pins survive a gesture and a reverse seek', () => {
  const breath = {id:'breath',duration:4,tracks:{'body.breath':[[0,0],[2,1],[4,0]]}}
  const hand = {id:'settle',duration:2,tracks:{'hand.left.wave':[[0,0],[1,1],[2,0]]}}
  const gesture = {id:'gesture',duration:1,tracks:{'hand.left.wave':[[0,0],[1,0]]}}
  const mixed = validateRig({...rig,clips:[breath,hand,gesture],idleMotion:{duration:4,
    layers:[{clip:'breath',weight:.8},{clip:'settle',weight:.2,offset:.5}]}})
  const at = (idle,elapsed,pins) => sampleLayers(mixed,[...idleLayers(mixed,idle),gestureLayer(gesture,elapsed)],pins)
  const before = at(4.5,-1)
  assert.equal(before['body.breath'],.2)
  assert.equal(before['hand.left.wave'],.2)
  assert.deepEqual(before,at(.5,1))
  assert.deepEqual(before,at(-3.5,2))
  assert.equal(at(4.5,.5)['body.breath'],.2)
  assert.equal(at(4.5,.5)['hand.left.wave'],0)
  assert.equal(at(4.5,.09)['hand.left.wave'],.1)
  assert.equal(at(4.5,.5,{'hand.left.wave':.75})['hand.left.wave'],.75)
  assert.deepEqual(at(4.5,0),before)
})

test('weights blend joints and preserve the neutral texture coordinates', () => {
  const layer = rig.layers[0]
  const rest = pose(rig, sample(rig, undefined, 0))
  const posed = new Map(rest)
  const matrix = [...rest.get('tail.end').matrix]
  matrix[4] += 10
  posed.set('tail.end', { matrix, opacity: 1 })
  const neutral = skin(layer, layer.mesh, rest, rest)
  const shifted = skin(layer, layer.mesh, rest, posed)
  assert.equal(shifted[0][0], neutral[0][0])
  assert.equal(shifted[1][0] - neutral[1][0], 7.5)
  assert.equal(shifted[2][0] - neutral[2][0], 10)
})

test('a layered tail swing advances one turn and meets the position and speed of the idle', () => {
  const swing = aikoClips.find(c => c.id === 'tail')
  const sway = restingClips.find(c => c.id === 'tail-sway')
  const phase = 'tail.curl'
  const r = validateRig({...rig,bones:[],layers:[],parameters:[{id:phase,min:-1,max:1,default:0}],
    clips:[sway,swing],idleMotion:{duration:6,layers:[{clip:sway.id}]}})
  const end = swing.phaseGesture.duration
  const circularStep = (a,b) => (b-a+3)%2-1
  for (const offset of [0,1.5,3.75]) {
    const idle = t => idleLayers(r,offset+t)
    const base = t => sampleLayers(r,idle(t))[phase]
    const mixed = t => sampleLayers(r,[...idle(t),gestureLayer(swing,t)])[phase]
    let travel = 0, previous = mixed(0)
    for (let frame=1;frame<=end*30;frame++) {
      const t = frame/30, next = mixed(t)
      const step = circularStep(previous,next)
      assert.ok(Math.abs(step)<.03, 'Large step at '+t)
      travel += step
      previous = next
    }
    assert.ok(Math.abs(travel-(base(end)-base(0))-2)<1e-9)
    for (const t of [0,end]) {
      assert.equal(mixed(t),base(t))
      for (const dt of [-1/30,1/30]) assert.ok(Math.abs(circularStep(base(t+dt),mixed(t+dt)))<.00001)
    }
    assert.equal(mixed(end+.5),base(end+.5))
  }
  assert.equal(sample(r,swing,0)[phase],-1)
  assert.equal(sample(r,swing,2)[phase],1)
  const bad = {...swing,tracks:{[phase]:[[0,-1],[2,.5]]}}
  assert.throws(() => validateRig({...r,clips:[sway,bad]}),/full turn/)
})

test('moving a rotated neutral elbow pivot keeps child artwork registered', () => {
  const copy = structuredClone(rig)
  const rest = sample(copy, undefined, 0)
  const before = pose(copy, rest)
  const arm = copy.bones.find(b => b.id === 'arm.left')
  movePivot(copy, arm, arm.x+7, arm.y+3)
  const after = pose(copy, rest)
  before.get('hand.left').matrix.forEach((v, i) =>
    assert.ok(Math.abs(v-after.get('hand.left').matrix[i]) < 1e-8))
})
