import type { Rig, Values } from './model.js'
import { draw } from './runtime.js'
import { createSurfaceRenderer } from './surface-renderer.js'

/** Bind a loaded rig to a fresh canvas. Render with absolute parameter values from sample(). */
export function createRigRenderer(canvas: HTMLCanvasElement, rig: Rig, images: Map<string,HTMLImageElement>) {
  if (rig.layers.some(layer => layer.surface)) {
    return {canvas,kind:'surface' as const,...createSurfaceRenderer(canvas,rig,images)}
  }
  const ctx = canvas.getContext('2d')
  if (!ctx) throw Error('A cutout rig needs a 2D canvas')
  return {canvas,kind:'cutout' as const,render:(values: Values, skeleton = false, _solo = '') =>
    draw(ctx,rig,images,values,skeleton),dispose() {}}
}
