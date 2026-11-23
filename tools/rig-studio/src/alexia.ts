import type { Clip, Parameter } from './model.js'

// Each source array specifies raw values at the public parameter's knots.
// This table is shared by the offline sampler and the optional comparison pane.
export interface Calibration extends Parameter { knots: number[]; source: Record<string, number[]> }
function control(id: string, label: string, knots: number[], neutral: number, source: Record<string, number[]>): Calibration {
  return {id,label,min:knots[0],max:knots[knots.length-1],default:neutral,knots,source}
}
const signed = [-1,0,1], angle = [-30,0,30]
function overlay(id: string, label: string, raw: string, group = 'Face overlays'): Calibration {
  return {...control(id,label,[0,.5,1],0,{[raw]:[0,15,30]}),group}
}
// One closed turn of the ambient tail wave. Offset each joint by its value at
// phase zero so the authored rest pose remains available in the inspector.
const tailPhase = Array.from({length:33},(_,i) => (i-16)/16)
const tailWave = Object.fromEntries(Array.from({length:5},(_,i) => {
  const lag = .7*i
  const degrees = tailPhase.map(phase => 10*(Math.sin(Math.PI*phase + lag) - Math.sin(lag)))
  degrees[degrees.length-1] = degrees[0]
  return ['Param_Angle_Rotation_'+(i+1)+'_ArtMesh202',degrees]
}))
export const wardrobe = 'outfit.style'
export const calibration: Calibration[] = [
  {...control(wardrobe,'Outfit',[0,1,2],0,{Param16:[0,30,30],Param17:[0,0,30]}),
    options:[{value:0,label:'Casual black'},{value:1,label:'Pajamas · hood up'},{value:2,label:'Pajamas · hood down'}]},
  control('eyes.look.x','Gaze horizontal',signed,0,{ParamEyeBallX:signed}),
  control('eyes.look.y','Gaze vertical',signed,0,{ParamEyeBallY:signed}),
  control('head.yaw','Head turn',angle,0,{ParamAngleX:angle}),
  control('head.pitch','Head pitch',[-20,0,20],0,{ParamAngleY:[-20,0,20]}),
  control('head.tilt','Head tilt',[-25,0,25],0,{ParamAngleZ:[-25,0,25]}),
  control('body.pitch','Torso pitch',angle,0,{ParamBodyAngleY:angle}),
  control('body.breath','Chest lift',[0,.5,1],0,{ParamBreath:[0,.5,1],Param10:[0,5,10]}),
  control('chest.sway','Upper-body sway',signed,0,{Param4:[-18,0,18]}),
  control('ear.left.flex','Left ear flex',signed,0,{Param18:angle}),
  control('ear.right.flex','Right ear flex',signed,0,{Param14:angle}),
  control('eyes.open','Eye opening',[0,.5,1],1,{ParamEyeLOpen:[0,.5,1],ParamEyeROpen:[0,.5,1]}),
  control('face.smile','Smile',[0,.5,1],0,{ParamMouthForm:[0,.5,1]}),
  {...control('mouth.open','Mouth opening',[0,.5,1],0,{ParamMouthOpenY:[0,.5,1]}),
    enabledWhen:{id:'face.grin',equals:0}},
  control('face.grin','Toothy grin',[0,.25,.5,.75,1],0,{Param54:[0,7.5,15,22.5,30]}),
  control('jaw.drop','Jaw drop',[0,.5,1],0,{Param50:[0,.5,1]}),
  control('brows.lift','Brow lift',signed,0,{ParamBrowLY:signed}),
  control('brows.form','Brow shape',signed,0,{ParamBrowLForm:signed}),
  overlay('face.blush','Blush','Param58'),
  overlay('face.tears','Tears','Param59'),
  overlay('face.shadow','Angry shadow','Param57'),
  overlay('symbol.question','Question mark','Param43'),
  overlay('symbol.sweat','Sweat drop','Param44'),
  {...control('eyes.style','Eye expression',[0,1,2],0,{Param55:[0,30,0],Param56:[0,0,30]}),
    group:'Face overlays',options:[{value:0,label:'Natural'},{value:1,label:'Stars'},{value:2,label:'Spirals'}]},
  {...overlay('eyes.left.purple','Left purple iris','Param62'),enabledWhen:{id:'eyes.style',equals:[0,1]}},
  {...overlay('eyes.right.purple','Right purple iris','Param63'),enabledWhen:{id:'eyes.style',equals:[0,1]}},
  overlay('prop.lollipop','Lollipop','Param60','Accessories'),
  overlay('glasses.face','Glasses on face','Param64','Accessories'),
  overlay('glasses.head','Sunglasses on head','Param11','Accessories'),
  {...control('arms.crossed','Casual arm pose',[0,1],0,{Param61:[0,30]}),
    enabledWhen:{id:wardrobe,equals:0},options:[{value:0,label:'Relaxed'},{value:1,label:'Crossed'}]},
  control('tail.curl','Tail swing phase',tailPhase,0,tailWave),
]
export const clips: Clip[] = [
  {id:'idle',label:'Breathing',duration:4,tracks:{'body.breath':[[0,0],[2,1],[4,0]]}},
  {id:'ears',label:'Ear flicks',duration:2,tracks:{
    'ear.left.flex':[[0,0],[.25,1],[.42,-.45],[.65,0]],
    'ear.right.flex':[[0,0],[.9,0],[1.1,1],[1.3,-.45],[1.6,0]]}},
  {id:'bow',label:'Small bow',duration:1.5,tracks:{
    'head.pitch':[[0,0],[.3,-20],[.9,-20],[1.5,0]],
    'body.pitch':[[0,0],[.3,-8],[.9,-6],[1.5,0]]}},
  {id:'nod',label:'Nod yes',duration:1,tracks:{
    'head.pitch':[[0,0],[.3,-15],[.6,5],[1,0]]}},
  {id:'shake',label:'Shake no',duration:1.4,tracks:{
    'head.yaw':[[0,0],[.35,-15],[.7,15],[1.05,-10],[1.4,0]]}},
  {id:'puzzled',label:'Puzzled tilt',duration:2,tracks:{
    'head.tilt':[[0,0],[.42,11],[1.3,11],[2,0]],
    'brows.lift':[[0,0],[.35,.55],[1.4,.55],[2,0]],
    'symbol.question':[[0,0],[.18,0],[.48,1],[1.3,1],[1.8,0]]}},
  {id:'look',label:'Look around',duration:4,tracks:{
    'eyes.look.x':[[0,0],[1,-1],[3,1],[4,0]],'head.yaw':[[0,0],[1,-8],[3,8],[4,0]],
    'eyes.look.y':[[0,0],[1,.4],[3,-.4],[4,0]]}},
  {id:'tail',label:'Tail swing',duration:2,tracks:{'tail.curl':[[0,-1],[2,1]]}},
  {id:'grin',label:'Toothy grin',duration:2,tracks:{
    'face.grin':[[0,0],[.4,1],[1.45,1],[2,0]],'jaw.drop':[[0,0],[.4,0],[.85,.8],[1.45,0],[2,0]]}},
]
