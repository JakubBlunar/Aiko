import http from 'node:http'
import path from 'node:path'
import { readFile, realpath, stat } from 'node:fs/promises'
import { createReadStream } from 'node:fs'
import { parseArgs } from 'node:util'
import { fileURLToPath } from 'node:url'
import { Worker } from 'node:worker_threads'
import { watchStudio } from './dev-watch.mjs'

const studio = fileURLToPath(new URL('.', import.meta.url))
const project = path.resolve(studio, '../..')
const { values } = parseArgs({ options: { port: {type: 'string', default: '5176'}, reference: {type: 'string'},
  rig:{type:'string'}, 'no-watch':{type:'boolean'} } })
const port = Number(values.port)
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid port')
const defaultModel = path.join(project, 'data/personas/active/Alexia/Alexia.model3.json')
const source = path.resolve(values.reference ?? defaultModel)
const rigDir = path.resolve(values.rig ?? path.join(studio,'private/aiko'))
let localFiles = new Set()
// The compiler and its TypeScript dependencies can be rebuilt while the studio
// is open. A fresh worker reads the current build without restarting the server.
let compiling
function readWardrobe() {
  if (!compiling) compiling = new Promise((resolve,reject) => {
    const worker = new Worker(new URL('./wardrobe.mjs',import.meta.url),{workerData:{dir:rigDir}})
    worker.once('message',resolve)
    worker.once('error',reject)
    worker.once('exit',code => { if (code) reject(Error('Wardrobe compiler exited with code '+code)) })
  }).finally(() => { compiling = undefined })
  return compiling
}
async function localManifest() {
  let hasWardrobe = true
  try { await within(rigDir,'wardrobe.json') } catch (e) {
    if (e.code !== 'ENOENT') throw e
    hasWardrobe = false
  }
  const bytes = hasWardrobe ? await readWardrobe() : await readFile(await within(rigDir,'rig.json'))
  const rig = JSON.parse(bytes)
  localFiles = new Set(rig.layers.map(layer => layer.src))
  return bytes
}
let hasLocal = false
try { await localManifest(); hasLocal = true } catch (e) { if (values.rig || e.code !== 'ENOENT') throw e }
const clients = new Set()
const publish = (event,data) => {
  for (const client of clients) client.write('event: ' + event + '\ndata: ' + JSON.stringify(data) + '\n\n')
}
const vendor = new Map([
  ['/vendor/pixi.js', path.join(project, 'web/node_modules/pixi.js/dist/browser/pixi.min.js')],
  ['/vendor/cubism.js', path.join(project, 'web/public/live2d/live2dcubismcore.min.js')],
  ['/vendor/adapter.js', path.join(project, 'web/node_modules/pixi-live2d-display/dist/cubism4.min.js')]
])
const mime = {'.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml',
  '.json':'application/json', '.png':'image/png', '.webp':'image/webp', '.jpg':'image/jpeg'}

async function within(root, name) {
  const file = await realpath(path.resolve(root, name))
  const relative = path.relative(await realpath(root), file)
  if (relative.startsWith('..' + path.sep) || relative === '..' || path.isAbsolute(relative)) throw new Error('Invalid asset path')
  return file
}

// Publish only the mesh and textures explicitly referenced by this model.
const assets = new Map()
let manifest, problem
try {
  const original = JSON.parse(await readFile(source, 'utf8'))
  const { Moc, Textures } = original.FileReferences
  if (!Moc.toLowerCase().endsWith('.moc3') || !Textures.length) throw new Error('Expected a Cubism 3+ mesh and textures')
  assets.set('/reference/model.moc3', await within(path.dirname(source), Moc))
  const textures = []
  for (const [i, texture] of Textures.entries()) {
    if (!/\.png$/i.test(texture)) throw new Error('Expected PNG reference textures')
    const name = 'texture-' + i + '.png'
    assets.set('/reference/' + name, await within(path.dirname(source), texture))
    textures.push(name)
  }
  manifest = { Version: 3, FileReferences: { Moc: 'model.moc3', Textures: textures }, Groups: [], HitAreas: [] }
  for (const file of vendor.values()) await stat(file)
} catch (error) {
  problem = error.code === 'ENOENT' && !values.reference ? 'Reference model or web dependencies unavailable' : error.message
}
const official = path.resolve(source) === path.resolve(defaultModel)
const routes = new Map([['/', 'index.html'], ['/style.css', 'style.css']])
const server = http.createServer(async (req, res) => {
  try {
    if (req.headers.host !== '127.0.0.1:' + port && req.headers.host !== 'localhost:' + port) {
      res.writeHead(403).end()
      return
    }
    const url = new URL(req.url, 'http://localhost').pathname
    if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405).end(); return }
    res.setHeader('Cache-Control', 'no-store')
    if (url === '/dev/events' && !values['no-watch']) {
      res.writeHead(200,{'Content-Type':'text/event-stream','Connection':'keep-alive'})
      res.write(': connected\n\n')
      clients.add(res)
      res.on('close', () => clients.delete(res))
      return
    }
    if (url === '/local/info') {
      res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify({available:hasLocal,watch:!values['no-watch']}))
      return
    }
    if (url === '/local/rig.json' && hasLocal) {
      const bytes = await localManifest()
      res.writeHead(200,{'Content-Type':'application/json'}).end(bytes)
      return
    }
    if (url === '/reference/info' || (url === '/reference/model.model3.json' && !problem)) {
      const info = {available: !problem, reason: problem, name: path.basename(source, '.model3.json'), alexia: official}
      res.writeHead(200, {'Content-Type':'application/json'}).end(JSON.stringify(url.endsWith('/info') ? info : manifest))
      return
    }
    let file = vendor.get(url) ?? (!problem && assets.get(url))
    if (url.startsWith('/local/') && hasLocal) {
      const src = decodeURIComponent(url.slice(7))
      if (localFiles.has(src) && /\.(png|webp|jpe?g|svg)$/i.test(src)) file = await within(rigDir,src)
    }
    if (routes.has(url)) file = path.join(studio, routes.get(url))
    if (/^\/(dist|example)\/[a-z0-9_.-]+$/i.test(url)) file = await within(path.join(studio, url.split('/')[1]), url.split('/')[2])
    if (!file || !(await stat(file)).isFile()) { res.writeHead(404).end(); return }
    res.writeHead(200, {'Content-Type': mime[path.extname(file)] ?? 'application/octet-stream'})
    if (req.method === 'HEAD') res.end()
    else createReadStream(file).on('error', () => res.destroy()).pipe(res)
  } catch { res.writeHead(404).end() }
})
server.listen(port, '127.0.0.1', () => {
  console.log('Rig Studio  http://127.0.0.1:' + port)
  if (!values['no-watch']) {
    const close = watchStudio(studio,hasLocal ? rigDir : undefined,publish)
    server.on('close',close)
  }
})
