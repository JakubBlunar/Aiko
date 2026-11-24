import type { Surface } from './surface.js'
export type Key = [position: number, value: number]
export type Channel = 'x' | 'y' | 'rotation' | 'scaleX' | 'scaleY' | 'opacity'
export interface Parameter {
  enabledWhen?: {id:string; equals:number | number[]}
  group?: string
  options?: {value: number; label: string}[]
  id: string
  label?: string
  min: number
  max: number
  default: number
}
export interface Driver { param: string; channel: Channel; keys: Key[] }
export interface Bone {
  id: string
  parent?: string
  /** Pivot relative to the parent pivot, in neutral artwork pixels. */
  x: number
  y: number
  scaleX?: number
  drives?: Driver[]
}
export interface Vertex { x: number; y: number; weights: Record<string, number> }
export interface Mesh { vertices: Vertex[]; triangles: [number, number, number][] }
export interface Layer {
  id: string; bone: string; src: string; x: number; y: number
  visibleIn?: number[]
  mesh?: Mesh; surface?: Surface; label?: string
}
export interface Clip {
  id: string; label?: string; duration: number; tracks: Record<string, Key[]>
  /** Play a complete cyclic track relative to the underlying pose as a layered gesture. */
  phaseGesture?: {parameter:string; duration:number}
}
export interface Rig {
  /** Preview cycle in seconds and the looping clips that make up the resting motion. */
  idleMotion?: { duration: number; layers: { clip: string; weight?: number; offset?: number }[] }
  variantParameter?: string
  attachments?:Record<string,string>
  costumeSettings?: Record<string,CostumeSettings>
  version: 1
  name: string
  width: number
  height: number
  parameters: Parameter[]
  bones: Bone[]
  /** Draw order, back to front. */
  layers: Layer[]
  clips: Clip[]
}
export interface CostumeSettings {
  defaults?: Values
  springs?: {input:string; output:string; gain:number; frequency:number; damping:number}[]
}
export type Values = Record<string, number>
export type Matrix = [number, number, number, number, number, number]
export interface BonePose { matrix: Matrix; opacity: number }
