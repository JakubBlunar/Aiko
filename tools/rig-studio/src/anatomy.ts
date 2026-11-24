import type { Bone, Rig } from './model.js'
import type { Surface } from './surface.js'
import { articulateHands } from './alexia-hands.js'

// A continuous strip can straddle a joint (pant leg, sock, hair, wrist).
// Keep its registered top fixed and transfer the lower vertices to the child.
function weightBelow(surface: Surface, bone: string, from: number, to: number) {
  const weights = surface.base.xy.filter((_,i) => i%2).map(y => {
    const t = Math.max(0,Math.min(1,(y-from)/(to-from)))
    return Math.round(t*t*(3-2*t)*1000)/1000
  })
  if (!weights.some(w => w > 0)) return
  if (!surface.skin) { surface.skin = {bone,weights}; return }
  // Distal joints take over the weight already assigned to their parent.
  const earlier = Array.isArray(surface.skin) ? surface.skin : [surface.skin]
  for (const influence of earlier) {
    influence.weights = influence.weights.map((w,i) => w*(1-weights[i]))
  }
  surface.skin = [...earlier,{bone,weights}]
}

// Registration landmarks in the 1125 × 2048 neutral study. Sides are the character's.
export function articulateAlexia(rig: Rig, inventory: {id:string; group:string}[]) {
  const sx = rig.width/1125, sy = rig.height/2048
  const pivots = new Map<string,[number,number]>([['root',[0,0]]])
  const add = (id: string, parent: string, x: number, y: number): Bone => {
    const [px,py] = pivots.get(parent)!
    pivots.set(id,[x*sx,y*sy])
    const bone = {id,parent,x:x*sx-px,y:y*sy-py}
    rig.bones.push(bone)
    return bone
  }
  function turn(bone: Bone, id: string, label: string, limit: number, sign = 1) {
    rig.parameters.push({id,label,min:-limit,max:limit,default:0})
    bone.drives = [{param:id,channel:'rotation',keys:[[-limit,-limit*sign],[limit,limit*sign]]}]
  }
  add('pelvis','root',562,915)
  add('torso','pelvis',562,665)
  add('head','torso',562,387)
  add('ear.left','head',635,144)
  add('ear.right','head',489,144)
  add('tail','pelvis',550,970)
  turn(add('ponytail','head',673,155),'hair.ponytail.sway','Ponytail sway',14)
  turn(add('ribbons','pelvis',562,855),'ribbons.sway','Waist ribbon sway',12)
  for (const side of ['left','right']) {
    add('arm.'+side,'torso',side === 'left' ? 709 : 415,487)
    const forearm = add('forearm.'+side,'arm.'+side,side === 'left' ? 736 : 387,720)
    const wrist = add('wrist.'+side,forearm.id,side === 'left' ? 844 : 280,921)
    const sign = side === 'left' ? 1 : -1
    const label = side === 'left' ? 'Left ' : 'Right '
    turn(forearm,'elbow.'+side+'.bend',label+'elbow bend',20,sign)
    turn(wrist,'hand.'+side+'.bend',label+'wrist bend',24,sign)
    const thigh = add('thigh.'+side,'pelvis',side === 'left' ? 639 : 486,948)
    const shin = add('shin.'+side,thigh.id,side === 'left' ? 630 : 494,1350)
    const foot = add('foot.'+side,shin.id,side === 'left' ? 625 : 500,1780)
    turn(thigh,'leg.'+side+'.shift',label+'leg shift',5,-sign)
    turn(shin,'knee.'+side+'.bend',label+'knee bend',9,sign)
    turn(foot,'foot.'+side+'.tilt',label+'foot tilt',12,sign)
  }
  for (const [i,layer] of rig.layers.entries()) {
    const s = layer.surface!
    const group = inventory[i].group.toLowerCase()
    const side = s.base.xy.filter((_,i) => !(i%2)).reduce((a,b) => a+b,0)/(s.base.xy.length/2)
      > rig.width/2 ? 'left' : 'right'
    let bone = 'root'
    if (group.includes('cat ear')) bone = 'ear.'+side
    else if (group.includes('cat tail')) bone = 'tail'
    else if (/\/ (?:left arm|right arm|motion arm|pajamas \/ sleeves)$/.test(group)) bone = 'arm.'+side
    else if (group.includes('/ part 55')) {
      bone = 'head'
      weightBelow(s,'ponytail',150*sy,205*sy)
    }
    // The ungrouped blue linings are visible exclusively with their matching hood.
    else if (group.endsWith('/ pajamas / hat / down') || layer.id === 'artmesh200') bone = 'torso'
    else if (group.includes('/ head') || group.includes('/ sunglasses') || group.includes('/ pajamas / hat')
      || group.includes('/ expressions') || group.includes('/ symbol expressions')
      || group.includes('/ layer 19 (skinning)') || ['artmesh129','artmesh201'].includes(layer.id)) bone = 'head'
    else if (group.includes('/ lower body / ribbon')) {
      bone = 'pelvis'
      if (group.includes('(skinning)')) weightBelow(s,'ribbons',850*sy,880*sy)
    }
    else if (/\/ lower body \/ (left|right)(?:$| \/)/.test(group) || group.endsWith('/ pajamas / legs')
      || group.endsWith('/ pajamas / feet')) {
      const top = Math.min(...s.base.xy.filter((_,i) => i%2))
      if (top > 1500*sy) {
        bone = 'shin.'+side
        weightBelow(s,'foot.'+side,1745*sy,1815*sy)
      } else {
        bone = 'thigh.'+side
        weightBelow(s,'shin.'+side,1230*sy,1440*sy)
      }
    }
    else if (group.includes('/ lower body') || layer.id === 'artmesh36') bone = 'pelvis'
    else if (group.includes('/ motions /') || layer.id === 'artmesh35') bone = 'torso'
    else if (group.includes('/ upper body') || group.endsWith('/ belly')) bone = 'torso'
    const [x,y] = pivots.get(bone)!
    layer.bone = bone
    layer.x = -x
    layer.y = -y
    if (bone === 'arm.'+side) {
      const [,wy] = pivots.get('wrist.'+side)!
      weightBelow(s,'forearm.'+side,655*sy,785*sy)
      weightBelow(s,'wrist.'+side,wy-18*sy,wy+28*sy)
    }
  }
  rig.clips.push({id:'wrist',label:'Wrist greeting',duration:2,tracks:{
    'elbow.left.bend':[[0,0],[.42,-16],[1.45,-16],[2,0]],
    'hand.left.bend':[[0,0],[.45,22],[.75,-14],[1.05,20],[1.4,-10],[2,0]]}})
  rig.clips.push({id:'foot-tap',label:'Foot tap',duration:1.4,tracks:{
    'leg.left.shift':[[0,0],[.45,2],[.95,2],[1.4,0]],
    'knee.left.bend':[[0,0],[.45,5],[.95,5],[1.4,0]],
    'foot.left.tilt':[[0,0],[.45,8],[.68,-3],[.95,6],[1.4,0]]}})
  articulateHands(rig)
}
