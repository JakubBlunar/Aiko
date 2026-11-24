import {copyFile, mkdir, mkdtemp, readFile, realpath, rename, rm, stat, writeFile} from 'node:fs/promises'
import path from 'node:path'
import {fileURLToPath, pathToFileURL} from 'node:url'
import {isMainThread, parentPort, workerData} from 'node:worker_threads'
import {execFileSync} from 'node:child_process'
import {existsSync} from 'node:fs'
import {separateAlexia} from './dist/alexia-body.js'
import {studioDress} from './dist/studio-dress.js'
import {composeWardrobe} from './dist/wardrobe.js'
import {validateRig} from './dist/runtime.js'

const studio=fileURLToPath(new URL('.',import.meta.url))
const readJson=async file=>JSON.parse(await readFile(file,'utf8'))
const saveJson=(file,value)=>writeFile(file,JSON.stringify(value)+'\n',{flag:'wx'})

async function inside(dir,name) {
  const root=await realpath(dir), file=await realpath(path.resolve(root,name))
  const relative=path.relative(root,file)
  if (!relative || relative.startsWith('..'+path.sep) || relative==='..' || path.isAbsolute(relative)) {
    throw Error('Expected a file inside the wardrobe: '+name)
  }
  return file
}

export async function loadWardrobe(dir) {
  const index=await readJson(path.join(dir,'wardrobe.json'))
  if (index.version!==1) throw Error('Unsupported wardrobe version')
  const read=async name=>readJson(await inside(dir,name))
  const rig=composeWardrobe(await read(index.body),await Promise.all(index.costumes.map(read)),index.initial)
  await Promise.all([...new Set(rig.layers.map(l=>l.src))].map(async src=>{
    if (!(await stat(await inside(dir,src))).isFile()) throw Error('Missing texture: '+src)
  }))
  return rig
}

async function compile(dir) {
  const rig=await loadWardrobe(dir)
  const stage=await mkdtemp(path.join(dir,'.compile-'))
  try {
    await saveJson(path.join(stage,'rig.json'),rig)
    await rename(path.join(stage,'rig.json'),path.join(dir,'rig.json'))
  } finally { await rm(stage,{recursive:true,force:true}) }
}

async function prepare(input,output) {
  const original=validateRig(await readJson(path.join(input,'rig.json')))
  const {body,stocks}=separateAlexia(original)
  const parent=path.dirname(output)
  await mkdir(parent,{recursive:true})
  try { await stat(output); throw Error('Choose a fresh wardrobe directory: '+output) }
  catch (error) { if (error.code!=='ENOENT') throw error }
  const stage=await mkdtemp(path.join(parent,'.wardrobe-'))
  try {
    for (const name of ['body','stock','dress','costumes']) await mkdir(path.join(stage,name))
    for (const src of new Set([...body.layers,...stocks.flatMap(c=>c.layers)].map(l=>l.src))) {
      const name=path.basename(src)
      if (src==='body/hair-clip.png') {
        const venv=path.resolve(studio,'../../.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python')
        const python=existsSync(venv)?venv:process.platform==='win32'?'python':'python3'
        const extract=['import sys','from PIL import Image',
          'with Image.open(sys.argv[1]) as source:',
          '    matte = Image.new("RGBA", source.size, "white")',
          '    matte.putalpha(source.convert("RGBA").getchannel("A"))',
          '    matte.save(sys.argv[2])'].join('\n')
        execFileSync(python,['-c',extract,await inside(input,'artmesh201.png'),path.join(stage,src)])
        continue
      }
      const art=src.endsWith('.svg') ? path.join(studio,'wardrobe-art',name) : await inside(input,name)
      await copyFile(art,path.join(stage,src))
    }
    const dress=studioDress(body.width)
    for (const name of new Set(dress.pieces.map(p=>p.src))) {
      await copyFile(path.join(studio,'wardrobe-art',path.basename(name)),path.join(stage,name))
    }
    await saveJson(path.join(stage,'body.json'),body)
    const costumes=[...stocks,{value:3,label:'Body fitting',pieces:[]},dress]
    const names=['casual','pajamas-hood-up','pajamas-hood-down','fitting','studio-dress']
    const files=names.map(n=>'costumes/'+n+'.json')
    for (const [i,costume] of costumes.entries()) await saveJson(path.join(stage,files[i]),costume)
    await saveJson(path.join(stage,'wardrobe.json'),{version:1,body:'body.json',costumes:files,initial:0})
    await compile(stage)
    await rename(stage,output)
  } finally { await rm(stage,{recursive:true,force:true}) }
}

if (!isMainThread) {
  parentPort.postMessage(JSON.stringify(await loadWardrobe(workerData.dir)))
} else if (process.argv[1] && import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href) {
  const [command,dir,output]=process.argv.slice(2)
  if (command==='prepare' && dir && output) await prepare(path.resolve(dir),path.resolve(output))
  else if (command==='compile' && dir) await compile(path.resolve(dir))
  else throw Error('Usage: node wardrobe.mjs prepare SOURCE NEW_DIRECTORY | compile DIRECTORY')
}
