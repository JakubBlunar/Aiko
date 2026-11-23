import test from 'node:test'
import assert from 'node:assert/strict'
import {deform,validateSurfaces} from '../dist/surface.js'
import {sample,validateRig} from '../dist/runtime.js'

function character() {
  const base = {xy:[0,0, 8,0, 0,8],opacity:.8,order:0,shapes:{
    'ear.flex':[{at:-1,offsets:[-2,0,0,0,0,0]},{at:0},{at:1,offsets:[4,0,0,0,0,0],opacity:.2}]
  }}
  return {version:1,name:'Example',width:10,height:10,variantParameter:'outfit',bones:[{id:'root',x:0,y:0}],
    parameters:[{id:'outfit',min:0,max:1,default:0,options:[{value:0,label:'A'},{value:1,label:'B'}]},
      {id:'ear.flex',min:-1,max:1,default:0,enabledWhen:{id:'outfit',equals:0}}],
    layers:[{id:'ear',bone:'root',src:'ear.png',x:0,y:0,surface:{base,blend:'normal',masks:[],
      indices:[0,1,2],uv:[0,0,1,0,0,1],variants:{1:{...base,order:3}}}}],clips:[]}
}
test('shape keys interpolate independent of frame order and retain the bind pose', () => {
  const rig = validateRig(character()), surface = rig.layers[0].surface
  assert.equal(deform(surface,{'ear.flex':.5}).xy[0],2)
  assert.equal(deform(surface,{'ear.flex':-1}).xy[0],-2)
  assert.equal(deform(surface,{'ear.flex':.5}).xy[0],2)
  assert.deepEqual(surface.base.xy,[0,0,8,0,0,8])
})
test('wardrobe options snap, choose their registered mesh and gate incompatible controls', () => {
  const rig = character(), values = sample(rig,undefined,0,{outfit:.8,'ear.flex':1})
  assert.deepEqual({...values},{outfit:1,'ear.flex':0})
  assert.equal(deform(rig.layers[0].surface,values,values.outfit).order,3)
})
test('a pinned tint returns when a compatible eye style is selected again', () => {
  const rig = character()
  rig.parameters.push({id:'style',min:0,max:2,default:0},
    {id:'tint',min:0,max:1,default:0,enabledWhen:{id:'style',equals:[0,1]}})
  validateRig(rig)
  for (const style of [0,1,2,1]) {
    assert.equal(sample(rig,undefined,0,{style,tint:.7}).tint,style === 2 ? 0 : .7)
  }
  rig.parameters.at(-1).enabledWhen.equals = []
  assert.throws(() => validateRig(rig),/Invalid parameter condition/)
})
test('invalid triangle, skin weight, missing mask and incomplete shape keys fail on import', () => {
  for (const breakSurface of [s => s.masks.push('missing'),s => s.indices.push(999),
    s => {s.skin = {bone:'root',weights:[0,2,0]}},
    s => {s.skin = [{bone:'root',weights:[.8,0,0]},{bone:'root',weights:[.3,0,0]}]},
    s => {s.base.shapes['ear.flex'][0].offsets = [1]}]) {
    const rig = character()
    breakSurface(rig.layers[0].surface)
    assert.throws(() => validateSurfaces(rig))
  }
})
