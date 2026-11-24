import assert from 'node:assert/strict'
import test from 'node:test'
import {fitGarment} from '../dist/garment.js'
import {deform} from '../dist/surface.js'
import {sample,sampleLayers} from '../dist/runtime.js'
import {composeWardrobe} from '../dist/wardrobe.js'

const torso={id:'torso',src:'skin.png',bone:'chest',x:-5,y:-8,surface:{
  base:{xy:[0,0,10,0,0,10],order:1,opacity:1,shapes:{breath:[{at:0},{at:1,offsets:[0,0,2,0,0,-4]}]}},
  blend:'normal',masks:[],uv:[0,0,1,0,0,1],indices:[0,1,2],skin:{bone:'arm',weights:[0,1,0]}}}
const body={version:1,name:'Fitting study',width:20,height:20,variantParameter:'outfit',
  bones:[{id:'chest',x:5,y:8},{id:'arm',parent:'chest',x:5,y:0}],
  parameters:[{id:'outfit',min:0,max:1,default:0},{id:'breath',min:0,max:1,default:0}],
  layers:[torso],attachments:{chest:'torso'},
  clips:[{id:'breathe',duration:2,tracks:{breath:[[0,0],[1,1],[2,0]]}}]}
const shirt={id:'front',label:'Shirt',src:'front.png',order:2,attach:'chest'}

test('a registered garment inherits body displacement and elbow weights, including beyond an edge',()=>{
  const mesh={xy:[2,3,12,0,0,12],uv:torso.surface.uv,indices:[0,1,2]}
  const sleeve=fitGarment(torso,{...shirt,mesh})
  const frame=deform(sleeve.surface,{breath:1})
  assert.ok(Math.abs(frame.xy[0]-2.4)<1e-5 && Math.abs(frame.xy[1]-1.8)<1e-5)
  assert.deepEqual(Array.from(frame.xy.slice(2)),[14,0,0,8])
  assert.deepEqual(sleeve.surface.skin[0].weights,[.2,1,0])
  assert.equal(sleeve.bone,torso.bone)
})

test('costumes own their masks, simulated motion and defaults and keep one skeleton',()=>{
  const dress={value:1,label:'Dress',pieces:[shirt,{...shirt,id:'trim',order:3,masks:['front']}],
    parameters:[{id:'hem',min:-5,max:5,default:0}],defaults:{hem:.1},
    springs:[{input:'breath',output:'hem',frequency:1,damping:.45,gain:1}]}
  const rig=composeWardrobe(body,[{value:0,label:'Body',pieces:[]},dress,{...dress,value:2,label:'Linen dress'}],0)
  assert.equal(body.layers.length,1)
  assert.equal(rig.bones.length,2)
  assert.deepEqual(rig.layers[2].surface.masks,['outfit.1.front'])
  const hem='outfit.1.hem'
  const pose=(t,overrides={})=>sample(rig,rig.clips[0],t,{outfit:1,...overrides})[hem]
  assert.equal(pose(0),.1)
  const halfway=pose(.5)
  assert.ok(halfway<.09)
  const hold={id:'hold',duration:1,tracks:{breath:[[0,0]]}}
  const layers=[{clip:rig.clips[0],seconds:.5},{clip:hold,seconds:.2,weight:.5}]
  const mixed=sampleLayers(rig,layers,{outfit:1})
  assert.equal(mixed.breath,.25)
  assert.ok(Math.abs(mixed[hem]-(.1+halfway)/2)<1e-10)
  const change={id:'change',duration:1,tracks:{outfit:[[0,2]]}}
  for (const [weight,value] of [[.49,0],[.5,2]]) {
    assert.equal(sampleLayers(rig,[{clip:change,seconds:0,weight}]).outfit,value)
  }
  pose(1.8)
  assert.equal(pose(.5),halfway)
  assert.equal(pose(.5,{[hem]:.8}),.8)
  assert.equal(pose(.5,{outfit:2}),0)
  assert.equal(pose(.5,{outfit:0}),0)
  assert.throws(()=>composeWardrobe(body,[{...dress,pieces:[{...shirt,attach:'missing'}]}],1),/attachment/)
})
