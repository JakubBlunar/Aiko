import type { Values } from './model.js'
export type Source = 'local' | 'example' | 'folder'
export interface Inspection {
  source: Source; clip: string; time: number; overrides: Values; playing: boolean
  skeleton: boolean; follow: boolean; gaze: Values; layer: string; reference: boolean
}

export function takeInspection(): Inspection | undefined {
  const key = 'rig-studio-inspection'
  const text = sessionStorage.getItem(key)
  sessionStorage.removeItem(key)
  return text ? JSON.parse(text) : undefined
}

// File inputs cannot be repopulated after navigation. Cache only the current tab's picked
// folder for the duration of a requested code reload, then remove its temporary entry.
export async function folderCache(action: 'save' | 'take', files?: Map<string,File>): Promise<Map<string,File> | undefined> {
  const key = sessionStorage.getItem('rig-studio-folder') ?? crypto.randomUUID()
  sessionStorage.setItem('rig-studio-folder',key)
  const db = await new Promise<IDBDatabase>((resolve,reject) => {
    const r = indexedDB.open('rig-studio-reload',1)
    r.onupgradeneeded = () => r.result.createObjectStore('folders')
    r.onsuccess = () => resolve(r.result)
    r.onerror = () => reject(r.error)
  })
  try {
    return await new Promise((resolve,reject) => {
      const tx = db.transaction('folders','readwrite')
      const store = tx.objectStore('folders')
      let restored: Map<string,File> | undefined
      if (action === 'save') store.put(files,key)
      else {
        const read = store.get(key)
        read.onsuccess = () => { restored = read.result; store.delete(key) }
      }
      tx.oncomplete = () => resolve(restored)
      tx.onabort = () => reject(tx.error)
    })
  } finally { db.close() }
}

export function watchChanges(inspect: () => Inspection, getFiles: () => Map<string,File> | undefined,
  reloadAsset: (state: Inspection) => Promise<void>, report: (message: string) => void) {
  const stream = new EventSource('/dev/events')
  let navigating = false
  stream.addEventListener('error',e => {
    if (e instanceof MessageEvent) report(JSON.parse(e.data))
  })
  stream.addEventListener('change',async e => {
    if (navigating) return
    try {
      const kinds: string[] = JSON.parse(e.data)
      const state = inspect()
      if (kinds.includes('code') || kinds.includes('page')) {
        navigating = true
        const files = getFiles()
        if (state.source === 'folder' && files) await folderCache('save',files)
        sessionStorage.setItem('rig-studio-inspection',JSON.stringify(state))
        location.reload()
        return
      }
      if (kinds.includes('css')) {
        const css = document.querySelector<HTMLLinkElement>('link[rel=stylesheet]')!
        css.href = '/style.css?rev=' + Date.now()
      }
      if (kinds.includes(state.source)) await reloadAsset(state)
    } catch (error) { navigating = false; report(String(error)) }
  })
}
