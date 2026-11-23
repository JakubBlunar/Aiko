import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import {movePivot, pose, sample, validateRig} from '../dist/runtime.js'
import {skin} from '../dist/mesh.js'

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
    r => { r.bones[1].drives[0].keys = [[1,0],[0,1]] }
  ]) {
    const bad = structuredClone(rig)
    corrupt(bad)
    assert.throws(() => validateRig(bad))
  }
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
