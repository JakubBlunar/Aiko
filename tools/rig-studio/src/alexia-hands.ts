import type { Key, Rig } from './model.js'
import type { SurfacePose } from './surface.js'
import { restingClips, restingMotion } from './alexia.js'

// Centerlines on Aiko's left hand in neutral artwork pixels. Reflect the
// registration for the right hand, including the separately meshed glove.
// The painted thumb crosses the ring finger; keep that overlap on the palm.
const fingers = [
  {name:'index',  x:888, y:967, endX:954, endY:1003, turn:14, shorten:.11},
  {name:'middle', x:891, y:986, endX:931, endY:1040, turn:17, shorten:.24},
  {name:'ring',   x:883, y:1004,endX:887, endY:1051, turn:12, shorten:.25},
  {name:'little', x:860, y:993, endX:840, endY:1029, turn:-17,shorten:.28},
] as const

function addBends(pose:SurfacePose, side:'left'|'right', width:number, height:number) {
  const sx=width/1125, sy=height/2048, mirror=side==='left' ? 1 : -1
  const offsets=fingers.map(()=>new Array<number>(pose.xy.length).fill(0))
  for (let i=0;i<pose.xy.length;i+=2) {
    const x=side==='left' ? pose.xy[i]/sx : (width-pose.xy[i])/sx
    const y=pose.xy[i+1]/sy
    if (y<960) continue
    let nearest=Infinity, digit=-1, along=0
    for (const [j,f] of fingers.entries()) {
      const dx=f.endX-f.x, dy=f.endY-f.y
      const u=((x-f.x)*dx+(y-f.y)*dy)/(dx*dx+dy*dy)
      const t=Math.max(0,Math.min(1,u))
      const distance=Math.hypot(x-f.x-t*dx,y-f.y-t*dy)
      if (distance<nearest) { nearest=distance; digit=j; along=u }
    }
    if (along<=0 || nearest>16) continue
    const f=fingers[digit], dx=f.endX-f.x, dy=f.endY-f.y
    // Bend progressively past the first knuckle, retaining finger thickness.
    const t=Math.min(1,along), angle=f.turn*Math.PI/180*t
    const shorten=1-f.shorten*t*t
    offsets[digit][i]=mirror*sx*along*((dx*Math.cos(angle)-dy*Math.sin(angle))*shorten-dx)
    offsets[digit][i+1]=sy*along*((dx*Math.sin(angle)+dy*Math.cos(angle))*shorten-dy)
  }
  for (const [j,f] of fingers.entries()) {
    pose.shapes['finger.'+side+'.'+f.name]=[{at:0},{at:1,offsets:offsets[j]}]
  }
}

export function articulateHands(rig:Rig) {
  for (const [id,side] of [['artmesh194','left'],['artmesh189','right'],['artmesh278','right']] as const) {
    const surface=rig.layers.find(l=>l.id===id)?.surface
    if (!surface) throw Error('Missing open-hand mesh: '+id)
    for (const pose of [surface.base,...Object.values(surface.variants ?? {})]) addBends(pose,side,rig.width,rig.height)
  }
  const settling:Record<string,Key[]>={}
  for (const side of ['left','right']) {
    for (const [index,f] of fingers.entries()) {
      const id='finger.'+side+'.'+f.name
      rig.parameters.push({id,label:(side==='left'?'Left ':'Right ')+f.name+' bend',
        group:'Fingers',min:0,max:1,default:0,enabledWhen:{id:'arms.crossed',equals:0}})
      const delay=.11*(3-index)+(side==='left' ? 0 : .3)
      settling[id]=[[0,0],[.12+delay,0],[.83+delay,.48+index*.15],
        [1.35+delay,.52+index*.15],[2.15+delay,0],[3,0]]
    }
  }
  rig.clips.push({id:'fingers',label:'Finger curl and release',duration:3,tracks:settling})
  rig.clips.push(...structuredClone(restingClips))
  rig.idleMotion = structuredClone(restingMotion)
  rig.clips.push({id:'beckon',label:'Beckon',duration:2.6,tracks:{
    'elbow.right.bend':[[0,0],[.4,-13],[1.95,-13],[2.6,0]],
    'hand.right.bend':[[0,0],[.45,16],[1.85,14],[2.6,0]],
    'finger.right.index':[[0,0],[.42,0],[.72,1],[1.02,0],[1.33,1],[1.7,0],[2.6,0]],
    'finger.right.middle':[[0,0],[.5,.87],[1.9,.8],[2.6,0]],
    'finger.right.ring':[[0,0],[.38,.95],[2,.9],[2.6,0]],
    'finger.right.little':[[0,0],[.3,1],[2.1,.86],[2.6,0]]
  }})
  const wave=rig.clips.find(c=>c.id==='wrist')!
  for (const [index,f] of fingers.entries()) {
    const delay=.07*(3-index)
    wave.tracks['finger.left.'+f.name]=[[0,0],[.32+delay,.48],[.62+delay,.08],
      [1+delay,.74],[1.33+delay,.18],[1.65+delay,.48],[2,0]]
  }
  const idle=rig.clips.find(c=>c.id==='idle')!
  for (const side of ['left','right']) for (const [index,f] of fingers.entries()) {
    const peak=1.3+.18*index+(side==='right'?.45:0)
    idle.tracks['finger.'+side+'.'+f.name]=[[0,0],[peak,.13+.065*index],[4,0]]
  }
}
