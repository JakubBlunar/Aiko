import {watch} from 'node:fs'
import {spawn} from 'node:child_process'
import path from 'node:path'

// Compile TypeScript once per settled edit batch. Only publish successfully built code.
export function watchStudio(studio, rigDir, publish) {
  const watchers = []
  const changes = new Set()
  let timer, busy = false, closed = false, compiler
  function schedule(kind) {
    if (closed) return
    changes.add(kind)
    clearTimeout(timer)
    timer = setTimeout(flush,150)
  }
  async function flush() {
    if (busy || closed || !changes.size) return
    busy = true
    const batch = new Set(changes)
    changes.clear()
    try {
      if (batch.has('code')) {
        const result = await new Promise(resolve => {
          compiler = spawn(process.execPath,[path.join(studio,'build.mjs')],{stdio:'inherit'})
          compiler.on('error',() => resolve(1))
          compiler.on('exit',resolve)
        })
        if (result !== 0) { publish('error','TypeScript build failed. See the studio terminal.'); return }
      }
      if (!closed) publish('change',[...batch])
    } finally {
      busy = false
      if (changes.size && !closed) timer = setTimeout(flush,150)
    }
  }
  function observe(dir, recursive, classify) {
    const watcher = watch(dir,{recursive},(_event,file) => {
      const kind = file && classify(String(file))
      if (kind) schedule(kind)
    })
    watcher.on('error',error => publish('error',error.message))
    watchers.push(watcher)
  }
  observe(path.join(studio,'src'),true,f => f.endsWith('.ts') && 'code')
  observe(studio,false,f => ({'index.html':'page','style.css':'css','tsconfig.json':'code'})[f])
  observe(path.join(studio,'example'),true,() => 'example')
  if (rigDir) observe(rigDir,true,f => /\.(json|png|webp|jpe?g|svg)$/i.test(f) && 'local')
  return () => { closed = true; clearTimeout(timer); compiler?.kill(); watchers.forEach(w => w.close()) }
}
