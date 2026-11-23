import type { Clip, Rig, Values } from './model.js'
import { clamp, loadImages, movePivot, parameterEnabled, sample, validateRig } from './runtime.js'
import { loadReference, type Reference } from './reference.js'
import { createRigRenderer } from './player.js'
import { folderCache, takeInspection, watchChanges, type Source, type Inspection } from './hot.js'

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T
const input = (id: string) => $<HTMLInputElement>(id)
const select = (id: string) => $<HTMLSelectElement>(id)
const button = (id: string) => $<HTMLButtonElement>(id)
const canvas = $<HTMLCanvasElement>('custom')
const atlas = $<HTMLCanvasElement>('atlas')
let renderer: ReturnType<typeof createRigRenderer> | undefined
let source: Source = 'example', picked: Map<string,File> | undefined, gaze: Values = {}
const seek = input('seek')
let rig: Rig, clip: Clip | undefined
let ref: Reference | undefined
let overrides: Values = {}, t = 0, playing = false, previous = 0, raf = 0
let loading = 0
const fields = new Map<string, {slider: HTMLInputElement | HTMLSelectElement; output: HTMLOutputElement; note: HTMLElement}>()
const fmt = (n: number) => Number(n.toFixed(3)).toString()
function errorText(e: unknown) { return e instanceof Error ? e.message : String(e) }

function render() {
  if (!rig || !renderer) return
  const values = sample(rig, clip, t, input('follow').checked ? {...overrides,...gaze} : overrides)
  const view = renderer.canvas
  const rect = view.getBoundingClientRect()
  const dpr = Math.min(devicePixelRatio, 2)
  const w = Math.max(1, Math.round(rect.width * dpr)), h = Math.max(1, Math.round(rect.height * dpr))
  if (view.width !== w || view.height !== h) { view.width = w; view.height = h }
  renderer.render(values,input('bones').checked,select('isolate').value)
  ref?.render(rig, values)
  for (const [id, f] of fields) {
    f.slider.value = String(values[id])
    const condition = rig.parameters.find(p => p.id === id)?.enabledWhen
    f.slider.disabled = !parameterEnabled(condition, values)
    f.output.value = fmt(values[id])
    f.note.textContent = [id, ref ? ref.mapped(id) ? 'reference linked' : 'custom only' : '',
      Object.hasOwn(overrides, id) ? 'manual' : ''].filter(Boolean).join(' · ')
  }
  seek.value = String(t)
  $('time').textContent = t.toFixed(3) + ' / ' + (clip?.duration ?? 0).toFixed(3) + ' s'
}

function stop() {
  playing = false
  cancelAnimationFrame(raf)
  button('play').textContent = 'Play'
}
function frame(now: number) {
  if (!playing || !clip) return
  t = (t + (now - previous) / 1000) % clip.duration
  previous = now
  render()
  raf = requestAnimationFrame(frame)
}
function chooseClip() {
  stop()
  if (!rig) return
  clip = rig.clips.find(c => c.id === select('motion').value)
  t = 0
  overrides = {}
  seek.max = String(clip?.duration ?? 0)
  for (const id of ['play','next','prev','seek']) input(id).disabled = !clip
  render()
}
button('play').onclick = () => {
  if (playing) { stop(); return }
  if (!clip) return
  previous = performance.now()
  playing = true
  button('play').textContent = 'Pause'
  raf = requestAnimationFrame(frame)
}
for (const [id, direction] of [['prev', -1], ['next', 1]] as const) {
  button(id).onclick = () => { stop(); t = clamp(t + direction / 30, 0, clip?.duration ?? 0); render() }
}
seek.oninput = () => { stop(); t = seek.valueAsNumber; render() }
select('motion').onchange = chooseClip
input('bones').onchange = render
select('isolate').onchange = render
input('follow').onchange = () => { gaze = {}; render() }
canvas.parentElement!.addEventListener('pointermove',event => {
  if (!input('follow').checked) return
  const rect = canvas.parentElement!.getBoundingClientRect()
  const x = clamp(2*(event.clientX-rect.left)/rect.width-1,-1,1)
  const y = clamp(1-2*(event.clientY-rect.top)/rect.height,-1,1)
  gaze = {'eyes.look.x':x,'eyes.look.y':y,'head.yaw':x*8,'head.pitch':y*5}
  render()
})
button('reset').onclick = () => { overrides = {}; render() }

function showJoint() {
  const b = rig?.bones.find(b => b.id === select('joint').value)
  if (!b) return
  input('jointX').value = fmt(b.x)
  input('jointY').value = fmt(b.y)
}
select('joint').onchange = showJoint
for (const [id, axis] of [['jointX','x'], ['jointY','y']] as const) {
  input(id).onchange = () => {
    const b = rig.bones.find(b => b.id === select('joint').value)!
    const value = input(id).valueAsNumber
    if (!Number.isFinite(value) || !b) { showJoint(); return }
    try { movePivot(rig, b, axis === 'x' ? value : b.x, axis === 'y' ? value : b.y) }
    catch (e) { $('status').textContent = errorText(e); showJoint(); return }
    stop()
    input('bones').checked = true
    render()
  }
}
function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
button('save').onclick = () => {
  if (rig) download(new Blob([JSON.stringify(rig, null, 2) + '\n'], {type:'application/json'}), 'rig.json')
}
button('neutral').onclick = async () => {
  if (!ref) return
  stop()
  button('neutral').disabled = true
  try {
    const png = ref.captureNeutral()
    render()
    download(await png, 'neutral.png')
  }
  catch (e) { $('status').textContent = errorText(e) }
  finally { button('neutral').disabled = false }
}

async function install(candidate: Rig, resolve: (s: string) => string, ticket: number) {
  validateRig(candidate)
  const loaded = await loadImages(candidate, resolve)
  if (ticket !== loading) return
  const next = createRigRenderer(candidate.layers.some(l => l.surface) ? atlas : canvas,candidate,loaded)
  renderer?.dispose()
  renderer = next
  canvas.hidden = next.kind === 'surface'
  atlas.hidden = !canvas.hidden
  stop()
  rig = candidate
  $('title').textContent = rig.name
  $('layerCount').textContent = rig.layers.length + ' layers'
  $('parameters').replaceChildren()
  const groups = new Map<string,HTMLFieldSetElement>()
  fields.clear()
  for (const p of rig.parameters) {
    const row = document.createElement('div')
    row.className = 'parameter'
    const head = document.createElement('div')
    head.className = 'row'
    const label = document.createElement('label')
    const slider = p.options ? document.createElement('select') : document.createElement('input')
    label.textContent = p.label ?? p.id
    label.htmlFor = slider.id = 'param-' + p.id
    const output = document.createElement('output'), note = document.createElement('span')
    note.className = 'note'
    head.append(label, output)
    if (slider instanceof HTMLSelectElement) {
      slider.append(...p.options!.map(o => new Option(o.label,String(o.value))))
    } else {
      slider.type = 'range'
      slider.min = String(p.min)
      slider.max = String(p.max)
      slider.step = 'any'
    }
    slider.oninput = () => { stop(); overrides[p.id] = Number(slider.value); render() }
    row.append(head, note, slider)
    if (p.group && !groups.has(p.group)) {
      const section = document.createElement('fieldset')
      const legend = document.createElement('legend')
      legend.textContent = p.group
      section.append(legend)
      $('parameters').append(section)
      groups.set(p.group,section)
    }
    const section = p.group ? groups.get(p.group)! : $('parameters')
    section.append(row)
    fields.set(p.id, {slider, output, note})
  }
  select('motion').replaceChildren(...rig.clips.map(c => new Option(c.label ?? c.id, c.id)), new Option('Rest pose', ''))
  select('joint').replaceChildren(...rig.bones.map(b => new Option(b.id,b.id)))
  $('isolateLabel').hidden = next.kind !== 'surface'
  select('isolate').replaceChildren(new Option('All visible pieces',''),
    ...rig.layers.filter(l => l.surface).map(l => new Option(l.label ?? l.id,l.id)))
  $('followLabel').hidden = !rig.parameters.some(p => p.id === 'eyes.look.x')
  input('follow').checked = false
  gaze = {}
  $('status').textContent = ''
  showJoint()
  chooseClip()
}

async function openFolder(files: Map<string,File>, ticket: number) {
  const urls: string[] = []
  try {
    const manifest = [...files.keys()].find(k => k.split('/').length === 2 && k.endsWith('/rig.json'))
    if (!manifest) throw new Error('Choose the folder that contains rig.json.')
    const root = manifest.slice(0, -'rig.json'.length)
    await install(JSON.parse(await files.get(manifest)!.text()), src => {
      const file = files.get(root + src)
      if (!file) throw new Error('Missing layer ' + src)
      const url = URL.createObjectURL(file)
      urls.push(url)
      return url
    }, ticket)
    if (ticket === loading) {
      picked = files
      source = 'folder'
      if (!select('source').querySelector('[value=folder]')) select('source').add(new Option('Imported folder','folder'))
      select('source').value = source
    }
  } finally {
    urls.forEach(url => URL.revokeObjectURL(url))
    input('folder').value = ''
  }
}
input('folder').onchange = () => {
  const files = new Map(Array.from(input('folder').files ?? [], f => [f.webkitRelativePath,f]))
  openFolder(files,++loading).catch(e => { $('status').textContent = errorText(e) })
}
async function openSource(which: Source, state?: Inspection) {
  const ticket = ++loading
  if (which === 'folder' && picked) await openFolder(picked,ticket)
  else {
    const base = which === 'local' ? '/local/' : '/example/'
    const res = await fetch(base+'rig.json')
    if (!res.ok) throw Error('Could not read ' + base + 'rig.json')
    await install(await res.json(),s => base+s.split('/').map(encodeURIComponent).join('/')+'?rev='+ticket,ticket)
  }
  if (ticket !== loading) return
  source = which
  if (source === 'local') select('source').querySelector<HTMLOptionElement>('[value=local]')!.text = rig.name
  select('source').value = source
  if (state) {
    select('motion').value = state.clip
    chooseClip()
    t = clamp(state.time,0,clip?.duration ?? 0)
    overrides = state.overrides
    input('bones').checked = state.skeleton
    input('follow').checked = state.follow && !$('followLabel').hidden
    gaze = state.gaze
    select('isolate').value = state.layer
    render()
    if (state.playing && clip) button('play').click()
  }
}
select('source').onchange = () => {
  openSource(select('source').value as Source).catch(e => {
    select('source').value = source
    $('status').textContent = errorText(e)
  })
}
function inspect(): Inspection {
  return {source,clip:clip?.id ?? '',time:t,overrides,playing,skeleton:input('bones').checked,
    follow:input('follow').checked,gaze,layer:select('isolate').value,reference:!!ref}
}
async function setupReference() {
  const info = await (await fetch('/reference/info')).json()
  $('refStatus').textContent = info.available ? 'Compare the same pose on ' + info.name + '.' : info.reason
  if (!info.available) return
  $('referenceName').textContent = info.name
  const load = button('loadRef')
  load.hidden = false
  load.onclick = async () => {
    load.disabled = true
    $('refStatus').textContent = 'Loading reference textures…'
    try {
      ref = await loadReference($<HTMLCanvasElement>('reference'), info.alexia)
      render()
      $('refPrompt').hidden = true
      button('neutral').hidden = false
    } catch (e) { $('refStatus').textContent = errorText(e); load.disabled = false }
  }
}
new ResizeObserver(render).observe(canvas.parentElement!)
document.addEventListener('visibilitychange', () => { if (document.hidden) stop() })
async function start() {
  const referenceReady = setupReference().catch(e => { $('refStatus').textContent = errorText(e) })
  const info = await (await fetch('/local/info')).json()
  if (info.available) select('source').add(new Option('Local character','local'))
  const state = takeInspection()
  if (state?.source === 'folder') picked = await folderCache('take')
  const initial = new URLSearchParams(location.search).get('rig') === 'example' ? 'example' : info.available ? 'local' : 'example'
  await openSource(state?.source ?? initial,state)
  if (info.watch) watchChanges(inspect,() => picked,state => openSource(source,state),s => { $('status').textContent = s })
  if (state?.reference) { await referenceReady; button('loadRef').click() }
}
start().catch(e => { $('status').textContent = errorText(e) })
