import { pattern, type GarmentPiece } from './garment.js'
import type { Costume } from './wardrobe.js'

// Registered sample patterns. Each panel has an independent texture and can
// be repainted or replaced in the exported costume without editing body.json.
export function studioDress(width:number):Costume {
  const sx=width/1125
  const pieces:GarmentPiece[]=[]
  function panel(id:string,attach:string,order:number,rows:[number,number,number][],fabric:string) {
    const mesh=pattern(rows.map(([y,l,r])=>[y,l*sx,r*sx]))
    const piece:GarmentPiece={id,label:'Studio dress · '+id,attach,order,mesh,src:'dress/'+fabric+'.svg'}
    pieces.push(piece)
    return piece
  }
  const front=panel('bodice','body.torso',76,[[407,521,604],[425,481,644],[452,440,685],
    [479,434,691],[520,439,686],[560,446,679],[600,449,676],[650,460,665],
    [690,471,654],[730,472,653],[760,468,657]],'bodice')
  const trim={...front,id:'embroidery',label:'Bodice embroidery',order:77,
    src:'dress/embroidery.svg',masks:['bodice']}
  pieces.push(trim)
  for (const side of ['left','right']) {
    const rows:[number,number,number][]=[[436,627,660],[449,616,682],[473,618,702],
      [500,627,718],[540,643,734],[578,652,750],[622,665,752],[670,683,758],
      [716,699,777],[761,712,800],[800,729,813]]
    panel('sleeve.'+side,'arm.'+side,65,
      side==='left'?rows:rows.map(([y,l,r])=>[y,1125-r,1125-l]),'sleeve')
  }
  for (const back of [true,false]) {
    const extra=back?12:0
    const hem=panel(back?'skirt.back':'skirt.front','body.torso',back?23:78,
      [[736,473,652],[763,463,662],[805,442,683],[860,424,701],[920,405,720],
        [990,385,740],[1050,365,760],[1102+extra,349-extra,776+extra]],back?'lining':'skirt')
    const offsets=hem.mesh!.uv.map((v,i,uv)=>i%2?0:(back?22:32)*uv[i+1]**2)
    hem.shapes={'dress.hem':[{at:-1,offsets:offsets.map(n=>-n)},{at:0},{at:1,offsets}]}
  }
  return {value:4,label:'Teal studio dress',pieces,parameters:[
    {id:'dress.hem',label:'Skirt swing',min:-1,max:1,default:0,group:'Costume'}
  ],defaults:{'dress.hem':.06},
  springs:[{input:'leg.left.shift',output:'dress.hem',gain:.32,frequency:1.7,damping:.34}]}
}
