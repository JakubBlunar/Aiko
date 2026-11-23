// A standalone checkout can install TypeScript locally. Aiko already has it in web/.
import { createRequire } from 'node:module'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
const require = createRequire(import.meta.url)
const compiler = require.resolve('typescript/bin/tsc', {
  paths: [fileURLToPath(new URL('.', import.meta.url)), fileURLToPath(new URL('../../web/', import.meta.url))]
})
const result = spawnSync(process.execPath, [compiler, '-p', fileURLToPath(new URL('tsconfig.json', import.meta.url))], {stdio: 'inherit'})
process.exitCode = result.status ?? 1
