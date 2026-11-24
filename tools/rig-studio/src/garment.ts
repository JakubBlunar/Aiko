import type { Layer } from './model.js'
import type { ShapeKey, SurfacePose } from './surface.js'

export interface Pattern { xy:number[]; uv:number[]; indices:number[] }
export interface GarmentPiece {
  id:string; label:string; src:string; attach:string; order:number
  mesh?:Pattern
  masks?:string[]
  inverted?:boolean
  shapes?:Record<string,ShapeKey[]>
}

// Closest point on the registered triangle mesh. Outside the body silhouette,
// a flared hem inherits the motion of the nearest edge without extrapolation.
function attachment(x:number,y:number,xy:number[],triangles:number[]) {
  let nearest = Infinity, binding = {indices:[0],weights:[1]}
  for (let i=0; i<triangles.length; i+=3) {
    const ids = triangles.slice(i,i+3), [a,b,c] = ids.map(k => [xy[2*k],xy[2*k+1]])
    const ux=b[0]-a[0], uy=b[1]-a[1], vx=c[0]-a[0], vy=c[1]-a[1]
    const det=ux*vy-uy*vx, px=x-a[0], py=y-a[1]
    if (Math.abs(det)>1e-8) {
      const u=(px*vy-py*vx)/det, v=(ux*py-uy*px)/det
      if (u>=0 && v>=0 && u+v<=1) return {indices:ids,weights:[1-u-v,u,v]}
    }
    for (let j=0; j<3; j++) {
      const from=ids[j], to=ids[(j+1)%3], ax=xy[2*from], ay=xy[2*from+1]
      const dx=xy[2*to]-ax, dy=xy[2*to+1]-ay
      const t=Math.max(0,Math.min(1,((x-ax)*dx+(y-ay)*dy)/(dx*dx+dy*dy || 1)))
      const dist=(ax+t*dx-x)**2+(ay+t*dy-y)**2
      if (dist<nearest) { nearest=dist; binding={indices:[from,to],weights:[1-t,t]} }
    }
  }
  return binding
}

export function fitGarment(body:Layer, piece:GarmentPiece):Layer {
  const source=body.surface
  if (!source) throw Error('Attachment requires an atlas mesh: '+body.id)
  const {base}=source
  const mesh=piece.mesh ?? {xy:base.xy,uv:source.uv,indices:source.indices}
  if (mesh.xy.length<6 || mesh.xy.length%2 || mesh.xy.length!==mesh.uv.length ||
    !mesh.xy.every(Number.isFinite) || !mesh.uv.every(Number.isFinite)) throw Error('Invalid pattern: '+piece.id)
  const bindings = piece.mesh ? mesh.xy.filter((_,i)=>i%2===0).map((x,i) =>
    attachment(x,mesh.xy[2*i+1],base.xy,source.indices)) : undefined
  function project(array:number[],stride:number) {
    return bindings ? bindings.flatMap(({indices,weights}) => Array.from({length:stride},(_,axis) =>
      indices.reduce((n,id,i) => n+array[id*stride+axis]*weights[i],0))) : array
  }
  const shapes:SurfacePose['shapes']={}
  for (const [id,keys] of Object.entries(base.shapes)) {
    if (keys.some(k=>k.offsets)) shapes[id]=keys.map(k=>k.offsets ?
      {at:k.at,offsets:project(k.offsets,2)} : {at:k.at})
  }
  for (const [id,keys] of Object.entries(piece.shapes ?? {})) {
    if (shapes[id]) throw Error('Cloth shape would replace inherited deformer: '+id)
    shapes[id]=keys
  }
  const skin=source.skin ? (Array.isArray(source.skin)?source.skin:[source.skin]).map(s=>({
    bone:s.bone,weights:project(s.weights,1)})) : undefined
  return {id:piece.id,label:piece.label,src:piece.src,bone:body.bone,x:body.x,y:body.y,
    surface:{base:{xy:mesh.xy,opacity:1,order:piece.order,shapes},uv:mesh.uv,indices:mesh.indices,
      skin,blend:'normal',masks:piece.masks ?? [],inverted:piece.inverted}}
}

// Horizontal pattern sections in neutral canvas pixels. Subdivision across the
// width also captures nonuniform chest motion from the underlying body mesh.
export function pattern(rows:[number,number,number][]):Pattern {
  const xy:number[]=[], uv:number[]=[], indices:number[]=[]
  const top=rows[0][0], height=rows[rows.length-1][0]-top
  for (const [r,[y,left,right]] of rows.entries()) for (let j=0;j<=4;j++) {
    xy.push(left+(right-left)*j/4,y)
    uv.push(j/4,(y-top)/height)
    const i=r*5+j
    if (r && j) indices.push(i-6,i-5,i,i-6,i,i-1)
  }
  return {xy,uv,indices}
}
