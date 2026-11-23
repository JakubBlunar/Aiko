import type { Layer, Matrix, Rig, Values } from './model.js'
import { compose, pose, sample } from './runtime.js'
import { deform } from './surface.js'

const vertex = `
attribute vec2 position;
attribute vec2 uv;
uniform vec2 fit;
uniform vec2 extent;
varying vec2 tex;
void main() {
  gl_Position = vec4(position * fit + vec2(-1.,1.) * extent, 0.,1.);
  gl_PointSize = 6.;
  tex = uv;
}`
const fragment = `
precision mediump float;
varying vec2 tex;
uniform sampler2D art;
uniform sampler2D matte;
uniform vec2 resolution;
uniform float opacity;
uniform float masking;
uniform float wire;
void main() {
  float a = 1.;
  if (masking != 0.) {
    a = texture2D(matte, gl_FragCoord.xy / resolution).a;
    if (masking < 0.) a = 1. - a;
  }
  gl_FragColor = wire > 1. ? vec4(.2,.9,1.,1.) : wire > 0. ? vec4(.8,.25,.1,.8) : texture2D(art, tex) * (a * opacity);
}`

/** GPU resources belong to one loaded character. Call dispose on replacement. */
export function createSurfaceRenderer(canvas: HTMLCanvasElement, rig: Rig, images: Map<string,HTMLImageElement>) {
  const options: WebGLContextAttributes = {alpha:true,antialias:true,premultipliedAlpha:true}
  const modern = canvas.getContext('webgl2',options)
  const context: WebGLRenderingContext | WebGL2RenderingContext | null = modern ?? canvas.getContext('webgl',options)
  if (!context) throw Error('This atlas rig needs WebGL')
  const gl = context
  const cleanup: (() => void)[] = []
  const dispose = () => { for (const release of cleanup.splice(0).reverse()) release() }
  function shader(type: number, code: string) {
    const object = gl.createShader(type)!
    cleanup.push(() => gl.deleteShader(object))
    gl.shaderSource(object,code)
    gl.compileShader(object)
    if (!gl.getShaderParameter(object,gl.COMPILE_STATUS)) throw Error(String(gl.getShaderInfoLog(object)))
    return object
  }
  function buffer(target: number, data: Float32Array | Uint16Array, usage: number) {
    const object = gl.createBuffer()!
    cleanup.push(() => gl.deleteBuffer(object))
    gl.bindBuffer(target,object)
    gl.bufferData(target,data,usage)
    return object
  }
  function texture() {
    const object = gl.createTexture()!
    cleanup.push(() => gl.deleteTexture(object))
    gl.bindTexture(gl.TEXTURE_2D,object)
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR)
    return object
  }

  try {
    const program = gl.createProgram()!
    cleanup.push(() => gl.deleteProgram(program))
    gl.attachShader(program,shader(gl.VERTEX_SHADER,vertex))
    gl.attachShader(program,shader(gl.FRAGMENT_SHADER,fragment))
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program,gl.LINK_STATUS)) throw Error(String(gl.getProgramInfoLog(program)))
    const uniform = (name: string) => gl.getUniformLocation(program,name)
    const u = {fit:uniform('fit'),extent:uniform('extent'),resolution:uniform('resolution'),
      opacity:uniform('opacity'),masking:uniform('masking'),wire:uniform('wire')}
    gl.useProgram(program)
    gl.uniform1i(uniform('art'),0)
    gl.uniform1i(uniform('matte'),1)
    const pos = gl.getAttribLocation(program,'position'), uv = gl.getAttribLocation(program,'uv')
    const textures = new Map<string,WebGLTexture>()
    gl.activeTexture(gl.TEXTURE0)
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL,true)
    for (const [src,image] of images) {
      const tex = texture()
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,image)
      if (modern) {
        gl.generateMipmap(gl.TEXTURE_2D)
        gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR_MIPMAP_LINEAR)
      }
      textures.set(src,tex)
    }
    const fbo = gl.createFramebuffer()!
    cleanup.push(() => gl.deleteFramebuffer(fbo))
    const matte = texture()
    // An unmasked pass still needs a complete second sampler distinct from the target.
    const blank = texture()
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,new Uint8Array(4))
    let mw = 0, mh = 0
    const neutral = sample(rig,undefined,0)
    const entries = rig.layers.map((layer: Layer) => {
      const surface = layer.surface!
      const lines = []
      for (let i=0; i<surface.indices.length; i+=3) {
        const [a,b,c] = surface.indices.slice(i,i+3)
        lines.push(a,b,b,c,c,a)
      }
      return {layer, surface, texture:textures.get(layer.src)!, frame:deform(surface,neutral),
        vertices:buffer(gl.ARRAY_BUFFER,new Float32Array(surface.uv.length),gl.DYNAMIC_DRAW),
        uvs:buffer(gl.ARRAY_BUFFER,new Float32Array(surface.uv),gl.STATIC_DRAW),
        triangles:buffer(gl.ELEMENT_ARRAY_BUFFER,new Uint16Array(surface.indices),gl.STATIC_DRAW),
        edges:buffer(gl.ELEMENT_ARRAY_BUFFER,new Uint16Array(lines),gl.STATIC_DRAW)}
    })
    const byId = new Map(entries.map(e => [e.layer.id,e]))
    const jointLines = buffer(gl.ARRAY_BUFFER,new Float32Array(4*rig.bones.length),gl.DYNAMIC_DRAW)
    function paint(e: typeof entries[number], maskPass: boolean, wire = false) {
      gl.bindBuffer(gl.ARRAY_BUFFER,e.vertices)
      gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0)
      gl.bindBuffer(gl.ARRAY_BUFFER,e.uvs)
      gl.vertexAttribPointer(uv,2,gl.FLOAT,false,0,0)
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,wire ? e.edges : e.triangles)
      gl.activeTexture(gl.TEXTURE0)
      gl.bindTexture(gl.TEXTURE_2D,e.texture)
      gl.uniform1f(u.opacity,maskPass ? 1 : e.frame.opacity)
      gl.uniform1f(u.masking,maskPass || !e.surface.masks.length ? 0 : e.surface.inverted ? -1 : 1)
      gl.uniform1f(u.wire,wire ? 1 : 0)
      const blend = maskPass || wire ? 'normal' : e.surface.blend
      gl.blendFuncSeparate(blend === 'multiply' ? gl.DST_COLOR : gl.ONE,
        blend === 'add' ? gl.ONE : gl.ONE_MINUS_SRC_ALPHA,blend === 'normal' ? gl.ONE : gl.ZERO,
        blend === 'normal' ? gl.ONE_MINUS_SRC_ALPHA : gl.ONE)
      gl.drawElements(wire ? gl.LINES : gl.TRIANGLES, e.surface.indices.length*(wire ? 2 : 1),gl.UNSIGNED_SHORT,0)
    }
    function render(values: Values, wire = false, solo = '') {
      const {width,height} = canvas
      gl.useProgram(program)
      gl.enableVertexAttribArray(pos)
      gl.enableVertexAttribArray(uv)
      gl.enable(gl.BLEND)
      gl.viewport(0,0,width,height)
      gl.uniform2f(u.resolution,width,height)
      const fit = Math.min(width/rig.width,height/rig.height)
      gl.uniform2f(u.fit,2*fit/width,-2*fit/height)
      gl.uniform2f(u.extent,rig.width*fit/width,rig.height*fit/height)
      gl.activeTexture(gl.TEXTURE1)
      if (mw !== width || mh !== height) {
        mw = width; mh = height
        gl.bindTexture(gl.TEXTURE_2D,matte)
        gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,width,height,0,gl.RGBA,gl.UNSIGNED_BYTE,null)
        gl.bindFramebuffer(gl.FRAMEBUFFER,fbo)
        gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,matte,0)
      }
      gl.bindTexture(gl.TEXTURE_2D,blank)
      gl.bindFramebuffer(gl.FRAMEBUFFER,null)
      gl.clearColor(0,0,0,0)
      gl.clear(gl.COLOR_BUFFER_BIT)
      const bones = pose(rig,values)
      const bind = pose(rig,neutral)
      for (const e of entries) {
        e.frame = deform(e.surface,values,rig.variantParameter ? values[rig.variantParameter] : undefined)
        const joint = bones.get(e.layer.bone)!
        const [a,b,c,d,tx,ty] = joint.matrix
        const skin = e.surface.skin
        const influences = (Array.isArray(skin) ? skin : skin ? [skin] : []).map(({bone,weights}) => {
          const [a,b,c,d,x,y] = bind.get(bone)!.matrix
          const det = a*d-b*c
          if (Math.abs(det) < 1e-10) throw Error('Singular bind matrix for ' + bone)
          const inverse: Matrix = [d/det,-b/det,-c/det,a/det,(c*y-d*x)/det,(b*x-a*y)/det]
          return {weights,matrix:compose(bones.get(bone)!.matrix,compose(inverse,bind.get(e.layer.bone)!.matrix))}
        })
        const xy = e.frame.xy
        for (let i=0; i<xy.length; i+=2) {
          const x = xy[i]+e.layer.x, y = xy[i+1]+e.layer.y
          const baseX = a*x+c*y+tx, baseY = b*x+d*y+ty
          xy[i] = baseX; xy[i+1] = baseY
          for (const {weights,matrix} of influences) {
            const w = weights[i/2], [e,f,g,h,u,v] = matrix
            xy[i] += w*(e*x+g*y+u-baseX)
            xy[i+1] += w*(f*x+h*y+v-baseY)
          }
        }
        e.frame.opacity *= joint.opacity
        gl.bindBuffer(gl.ARRAY_BUFFER,e.vertices)
        gl.bufferSubData(gl.ARRAY_BUFFER,0,xy)
      }
      let active = ''
      for (const e of [...entries].sort((a,b) => a.frame.order-b.frame.order)) {
        if (!e.frame.opacity || (solo && e.layer.id !== solo)) continue
        const masks = e.surface.masks
        const key = masks.join('|')
        if (key && key !== active) {
          gl.activeTexture(gl.TEXTURE1)
          gl.bindTexture(gl.TEXTURE_2D,blank)
          gl.bindFramebuffer(gl.FRAMEBUFFER,fbo)
          gl.clear(gl.COLOR_BUFFER_BIT)
          for (const id of masks) paint(byId.get(id)!,true)
          gl.bindFramebuffer(gl.FRAMEBUFFER,null)
          gl.activeTexture(gl.TEXTURE1)
          gl.bindTexture(gl.TEXTURE_2D,matte)
          active = key
        }
        paint(e,false)
        if (wire) paint(e,false,true)
      }
      if (wire && !solo) {
        const lines: number[] = []
        for (const b of rig.bones) {
          if (!b.parent || b.parent === 'root') continue
          const parent = bones.get(b.parent)!.matrix, joint = bones.get(b.id)!.matrix
          lines.push(parent[4],parent[5],joint[4],joint[5])
        }
        gl.bindBuffer(gl.ARRAY_BUFFER,jointLines)
        gl.bufferSubData(gl.ARRAY_BUFFER,0,new Float32Array(lines))
        gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0)
        gl.disableVertexAttribArray(uv)
        gl.vertexAttrib2f(uv,0,0)
        gl.uniform1f(u.masking,0)
        gl.uniform1f(u.wire,2)
        gl.blendFunc(gl.ONE,gl.ONE_MINUS_SRC_ALPHA)
        gl.drawArrays(gl.LINES,0,lines.length/2)
        gl.drawArrays(gl.POINTS,0,lines.length/2)
      }
    }
    return {render,dispose}
  } catch (error) { dispose(); throw error }
}
