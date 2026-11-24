import type { Clip, CostumeSettings, Layer, Parameter, Rig } from './model.js'
import type { SurfacePose } from './surface.js'
import { fitGarment, type GarmentPiece } from './garment.js'
import { validateRig } from './runtime.js'

export interface Costume extends CostumeSettings {
  value:number
  label:string
  pieces:GarmentPiece[]
  parameters?:Parameter[]
  clips?:Clip[]
  hideBody?:string[]
  // An imported costume can carry its original posed geometry and supply
  // alternate registration for body artwork (e.g. hair under a raised hood).
  layers?:Layer[]
  bodyPoses?:Record<string,SurfacePose>
}

export function composeWardrobe(body:Rig, costumes:Costume[], initial:number):Rig {
  const rig=structuredClone(body), outfit=rig.parameters.find(p=>p.id===rig.variantParameter)
  if (!outfit || !costumes.length) throw Error('A wardrobe needs a costume selector')
  const values=costumes.map(c=>c.value)
  outfit.min=Math.min(...values); outfit.max=Math.max(...values); outfit.default=initial
  outfit.options=costumes.map(c=>({value:c.value,label:c.label}))
  const base=new Map(rig.layers.map(l=>[l.id,l]))
  rig.costumeSettings={}
  for (const costume of costumes) {
    const {value,hideBody=[],bodyPoses={},parameters=[],clips=[],pieces,layers=[]}=costume
    const localParams=new Map(parameters.map(p=>[p.id,'outfit.'+value+'.'+p.id]))
    const paramId=(id:string)=>localParams.get(id) ?? id
    function scope<T>(record:Record<string,T>) {
      return Object.fromEntries(Object.entries(record).map(([id,v])=>[paramId(id),v]))
    }
    for (const id of hideBody) {
      const layer=base.get(id)
      if (!layer) throw Error('Unknown body coverage: '+id)
      layer.visibleIn=(layer.visibleIn ?? values).filter(v=>v!==value)
    }
    for (const [id,pose] of Object.entries(bodyPoses)) {
      const surface=base.get(id)?.surface
      if (!surface) throw Error('Unknown body registration: '+id)
      surface.variants ??= {}
      surface.variants[value]=pose
    }
    rig.parameters.push(...parameters.map(p=>({...p,id:paramId(p.id),enabledWhen:{id:outfit.id,equals:value}})))
    rig.clips.push(...clips.map(c=>({...c,id:'outfit.'+value+'.'+c.id,
      phaseGesture:c.phaseGesture && {...c.phaseGesture,parameter:paramId(c.phaseGesture.parameter)},
      tracks:{...scope(c.tracks),[outfit.id]:[[0,value] as [number,number]]}})))
    rig.costumeSettings[value]={defaults:scope(costume.defaults ?? {}),
      springs:costume.springs?.map(s=>({...s,input:paramId(s.input),output:paramId(s.output)}))}
    const garments=[...layers,...pieces.map(piece=>{
      const attach=base.get(body.attachments?.[piece.attach] ?? piece.attach)
      if (!attach) throw Error('Unknown body attachment: '+piece.attach)
      const pose=bodyPoses[attach.id]
      const target=pose && attach.surface ? {...attach,surface:{...attach.surface,base:pose}} : attach
      return fitGarment(target,{...piece,shapes:scope(piece.shapes ?? {})})
    })]
    const ids=new Map(garments.map(l=>[l.id,'outfit.'+value+'.'+l.id]))
    for (const layer of garments) {
      if (!layer.surface) throw Error('Missing garment surface: '+layer.id)
      rig.layers.push({...layer,id:ids.get(layer.id)!,visibleIn:[value],
        surface:{...layer.surface,masks:layer.surface.masks.map(id=>ids.get(id) ?? id)}})
    }
  }
  return validateRig(rig)
}
