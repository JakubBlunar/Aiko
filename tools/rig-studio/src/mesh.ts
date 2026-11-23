import type { BonePose, Layer, Matrix, Mesh } from './model.js'

type Point = [number, number]
const point = (m: Matrix, x: number, y: number): Point =>
  [m[0]*x + m[2]*y + m[4], m[1]*x + m[3]*y + m[5]]

function local(m: Matrix, x: number, y: number): Point {
  const det = m[0]*m[3] - m[1]*m[2]
  if (Math.abs(det) < 1e-10) throw new Error('A mesh joint has zero scale in the bind pose')
  const u = x - m[4], v = y - m[5]
  return [(m[3]*u - m[2]*v) / det, (m[0]*v - m[1]*u) / det]
}

/** Convert each vertex to its influencing joints' local spaces, then blend posed positions. */
export function skin(layer: Layer, mesh: Mesh, bind: Map<string, BonePose>, posed: Map<string, BonePose>): Point[] {
  const anchor = bind.get(layer.bone)!.matrix
  return mesh.vertices.map(({x, y, weights}) => {
    const neutral = point(anchor, layer.x + x, layer.y + y)
    let px = 0, py = 0
    for (const [id, weight] of Object.entries(weights)) {
      const joint = local(bind.get(id)!.matrix, ...neutral)
      const [u, v] = point(posed.get(id)!.matrix, ...joint)
      px += u * weight
      py += v * weight
    }
    return [px, py]
  })
}

export function drawMesh(ctx: CanvasRenderingContext2D, image: HTMLImageElement, mesh: Mesh,
  points: Point[], wireframe: boolean) {
  for (const [i, j, k] of mesh.triangles) {
    const [p, q, r] = [mesh.vertices[i], mesh.vertices[j], mesh.vertices[k]]
    const u = q.x-p.x, v = q.y-p.y, s = r.x-p.x, t = r.y-p.y
    const det = u*t - s*v
    const [[x,y],[x1,y1],[x2,y2]] = [points[i],points[j],points[k]]
    const a = ((x1-x)*t - (x2-x)*v) / det, b = ((y1-y)*t - (y2-y)*v) / det
    const c = ((x2-x)*u - (x1-x)*s) / det, d = ((y2-y)*u - (y1-y)*s) / det
    ctx.save()
    ctx.beginPath()
    ctx.moveTo(x,y)
    ctx.lineTo(x1,y1)
    ctx.lineTo(x2,y2)
    ctx.closePath()
    ctx.clip()
    ctx.transform(a,b,c,d, x-a*p.x-c*p.y, y-b*p.x-d*p.y)
    ctx.drawImage(image, 0, 0)
    ctx.restore()
    if (wireframe) {
      ctx.strokeStyle = '#d37248'
      ctx.lineWidth = .7
      ctx.stroke()
    }
  }
}
