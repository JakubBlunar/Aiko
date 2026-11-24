import type { Layer, Rig } from './model.js'
import { fitGarment, pattern } from './garment.js'
import type { Costume } from './wardrobe.js'
import type { SurfacePose } from './surface.js'

type Row = [number,number,number]
function mirrored(rows:Row[]):Row[] { return rows.map(([y,l,r])=>[y,1125-r,1125-l]) }

function clippingPose(pose:SurfacePose):SurfacePose {
  const shapes=Object.fromEntries(Object.entries(pose.shapes).map(([id,keys])=>
    [id,keys.map(({at,offsets})=>({at,offsets}))]))
  return {...pose,opacity:0,shapes}
}

export function separateAlexia(source:Rig) {
  const body:Rig={...structuredClone(source),layers:[]}
  const originals=new Map(source.layers.map(l=>[l.id,l]))
  const fits:Layer[]=[]
  function add(id:string, label:string, attach:string, rows:Row[], order:number, art:string) {
    const piece=fitGarment(originals.get(attach)!,{id:'body.'+id,label,attach,order,
      src:'body/'+art+'.svg',mesh:pattern(rows.map(([y,l,r])=>[y,l*body.width/1125,r*body.width/1125]))})
    fits.push(piece)
  }
  // Missing outline under the original shirt, trousers and footwear. Sections
  // overlap at joints; matching bones carry both sides through the overlap.
  add('torso','Fitting torso','artmesh159',[
    [407,525,600],[425,486,639],[449,447,678],[474,436,689],[515,442,683],
    [555,451,674],[590,453,672],[630,460,665],[682,465,660],[738,465,660],
    [780,457,668],[825,445,680],[869,436,689],[914,443,682],[957,475,650],[976,514,611]
  ],61,'torso')
  const leg:Row[]=[[850,552,636],[879,548,665],[925,551,683],[974,562,691],
    [1040,565,690],[1130,571,681],[1230,582,667],[1310,592,658],[1350,595,654],
    [1400,593,658],[1470,587,665],[1535,586,663],[1620,598,651],
    [1700,607,643],[1770,610,640],[1820,610,641]]
  const foot:Row[]=[[1700,609,642],[1760,610,640],[1810,607,644],[1854,602,650],
    [1875,598,651],[1900,593,654],[1915,592,652],[1925,596,648],[1930,605,639]]
  const shoulder:Row[]=[[433,634,647],[442,619,668],[462,618,690],[488,630,710],
    [512,643,722],[539,655,733],[565,663,738],[588,670,743],[611,677,744]]
  for (const side of ['left','right']) {
    const left=side==='left'
    const rows=(r:Row[])=>left?r:mirrored(r)
    add('leg.'+side,'Fitting leg · '+side,left?'artmesh175':'artmesh179',rows(leg),38,'leg')
    add('foot.'+side,'Fitting foot · '+side,left?'artmesh177':'artmesh181',rows(foot),37,'foot')
    add('shoulder.'+side,'Fitting upper arm · '+side,left?'artmesh194':'artmesh278',rows(shoulder),33,'skin')
  }
  const stocks:Costume[]=source.parameters.find(p=>p.id===source.variantParameter)!.options!.map(o=>({
    ...o,pieces:[],layers:[],bodyPoses:{},hideBody:fits.map(l=>l.id)
  }))
  for (const layer of source.layers) {
    const s=layer.surface!, group=layer.label ?? ''
    const anatomy=/\/ (Head|Left cat ear|Right cat ear|Cat tail|Part 55|Layer 19|Symbol expressions|Expressions|Sunglasses)\b/i.test(group)
      || ['artmesh194','artmesh278','artmesh162','artmesh161','artmesh160','artmesh186',
        'artmesh129','artmesh34'].includes(layer.id)
    // The original hair uses the hood outline as a matte even in casual wear.
    // Give the body its own alpha-only matte; the painted lining belongs to pajamas.
    if (layer.id==='artmesh201') {
      body.layers.push({...layer,id:'body.hair-clip',label:'Hair clipping silhouette',src:'body/hair-clip.png',
        surface:{...s,base:clippingPose(s.base),variants:undefined}})
      for (const costume of stocks) {
        const pose=s.variants?.[costume.value]
        if (pose) costume.bodyPoses!['body.hair-clip']=clippingPose(pose)
      }
    }
    const neutral=layer.id==='artmesh278' ? (s.variants?.['2'] ?? s.base) : s.base
    const src=(anatomy?'body/':'stock/')+layer.src
    if (anatomy) body.layers.push({...layer,src,surface:{...s,base:neutral,variants:undefined,
      masks:s.masks.map(id=>id==='artmesh201'?'body.hair-clip':id)}})
    for (const costume of stocks) {
      const posed=s.variants?.[costume.value] ?? s.base
      if (anatomy) {
        if (posed!==neutral) costume.bodyPoses![layer.id]=posed
      } else {
        costume.layers!.push({...layer,src,surface:{...s,base:posed,variants:undefined}})
      }
    }
  }
  body.layers.push(...fits)
  body.attachments = {torso:'body.torso',abdomen:'artmesh186',neck:'artmesh161',
    'arm.left':'artmesh194','arm.right':'artmesh278'}
  for (const side of ['left','right']) for (const part of ['leg','foot','shoulder']) {
    body.attachments[part+'.'+side]='body.'+part+'.'+side
  }
  for (const costume of stocks) {
    const layers=costume.layers!
    const required=new Set(layers.flatMap(l=>l.surface!.masks))
    costume.layers=layers.filter(l=>{
      const pose=l.surface!.base
      return required.has(l.id) || pose.opacity>0 ||
        Object.values(pose.shapes).some(keys=>keys.some(k=>(k.opacity ?? 0)>0))
    })
  }
  return {body,stocks}
}
