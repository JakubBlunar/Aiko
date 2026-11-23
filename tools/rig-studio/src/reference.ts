import type { Rig, Values } from './model.js'
import { calibration, wardrobe } from './alexia.js'
import { interpolate } from './runtime.js'
import type { Key } from './model.js'

// The optional reference adapter is the only browser module that knows Cubism.
interface Core {
  getModel(): { parameters: { ids: string[] } }
  getParameterMinimumValue(i: number): number
  getParameterMaximumValue(i: number): number
  getParameterDefaultValue(i: number): number
  setParameterValueByIndex(i: number, value: number): void
  update(): void
}
interface ReferenceModel {
  width: number
  height: number
  anchor: { set(x: number): void }
  scale: { set(x: number): void }
  position: { set(x: number, y: number): void }
  internalModel: { coreModel: Core }
}
interface Pixi {
  Application: new (options: object) => {
    stage: { addChild(model: ReferenceModel): void }
    renderer: { resize(w: number, h: number): void; gl: WebGLRenderingContext }
    render(): void
    destroy(removeView: boolean, options: object): void
  }
  live2d: { Live2DModel: { from(url: string, options: object): Promise<ReferenceModel> } }
}
declare global { interface Window { PIXI: Pixi } }

export interface Reference {
  mapped(id: string): boolean
  render(rig: Rig, values: Values): void
  captureNeutral(): Promise<Blob>
}

async function script(src: string) {
  await new Promise<void>((resolve, reject) => {
    const el = document.createElement('script')
    el.onload = () => resolve()
    el.onerror = () => reject(new Error('Could not load ' + src))
    el.src = src
    document.head.append(el)
  })
}

interface Binding { id: string; raw: string; low: number; high: number }
const bindings: Binding[] = [
  {id:'head.tilt', raw:'ParamAngleZ', low:-25, high:25},
  {id:'body.breath', raw:'ParamBreath', low:0, high:1},
  {id:'eyes.open', raw:'ParamEyeLOpen', low:0, high:1},
  {id:'eyes.open', raw:'ParamEyeROpen', low:0, high:1},
  {id:'face.smile', raw:'ParamMouthForm', low:0, high:1},
]

export async function loadReference(canvas: HTMLCanvasElement, alexia: boolean): Promise<Reference> {
  for (const src of ['pixi','cubism','adapter']) await script('/vendor/' + src + '.js')
  const app = new window.PIXI.Application({view:canvas, autoStart:false, backgroundAlpha:0,
    antialias:true, width:canvas.width, height:canvas.height})
  try {
    const model = await window.PIXI.live2d.Live2DModel.from('/reference/model.model3.json',
      {autoUpdate:false, autoInteract:false, motionPreload:'NONE'})
    if (app.renderer.gl.getParameter(app.renderer.gl.MAX_TEXTURE_SIZE) < 8192 && alexia) {
      throw new Error('Alexia requires support for 8192-pixel textures on this GPU')
    }
    const w = model.width, h = model.height
    model.anchor.set(.5)
    app.stage.addChild(model)
    const core = model.internalModel.coreModel
    const ids = core.getModel().parameters.ids
    const configured = [...bindings]
    if (alexia) {
      for (let joint = 1; joint <= 5; joint++) {
        const raw = 'Param_Angle_Rotation_' + joint + '_ArtMesh202'
        const index = ids.indexOf(raw)
        if (index >= 0) configured.push({id:'tail.curl', raw,
          low:core.getParameterMinimumValue(index), high:core.getParameterMaximumValue(index)})
      }
    }
    const mapped = configured.map(b => ({...b, index: ids.indexOf(b.raw)})).filter(b => b.index >= 0)
    const measured = calibration.flatMap(c => Object.entries(c.source).map(([raw, points]) => ({
      id:c.id, index:ids.indexOf(raw), keys:c.knots.map((v,i): Key => [v,points[i]])
    }))).filter(b => b.index >= 0)
    const reset = new Set([...mapped, ...measured].map(b => b.index))
    let useCalibration = false
    function fit(width: number, height: number, margin: number) {
      if (canvas.width !== width || canvas.height !== height) app.renderer.resize(width, height)
      model.scale.set(margin * Math.min(width / w, height / h))
      model.position.set(width / 2, height / 2)
    }
    return {
      mapped: id => (useCalibration ? measured : mapped).some(b => b.id === id),
      captureNeutral() {
        // The caller redraws the inspected pose after the PNG has been captured.
        for (const index of reset) core.setParameterValueByIndex(index, core.getParameterDefaultValue(index))
        const factor = 2048 / Math.max(w, h)
        fit(Math.max(1, Math.round(w*factor)), Math.max(1, Math.round(h*factor)), 1)
        core.update()
        app.render()
        return new Promise((resolve, reject) => canvas.toBlob(blob => {
          if (blob) resolve(blob)
          else reject(new Error('Could not export the neutral reference'))
        }, 'image/png'))
      },
      render(rig, values) {
        const rect = canvas.getBoundingClientRect()
        const width = Math.max(1, Math.round(rect.width * Math.min(devicePixelRatio, 2)))
        const height = Math.max(1, Math.round(rect.height * Math.min(devicePixelRatio, 2)))
        fit(width, height, 1)
        useCalibration = alexia && rig.variantParameter === wardrobe
        for (const index of reset) core.setParameterValueByIndex(index, core.getParameterDefaultValue(index))
        if (useCalibration) {
          for (const b of measured) if (Object.hasOwn(values,b.id)) {
            core.setParameterValueByIndex(b.index,interpolate(b.keys,values[b.id]))
          }
        } else for (const b of mapped) {
          const p = rig.parameters.find(p => p.id === b.id)
          const value = p ? b.low + (b.high - b.low) * (values[p.id] - p.min) / (p.max - p.min)
            : core.getParameterDefaultValue(b.index)
          core.setParameterValueByIndex(b.index, value)
        }
        core.update()
        app.render()
      }
    }
  } catch (error) {
    app.destroy(false, {children:true, texture:true, baseTexture:true})
    throw error
  }
}
