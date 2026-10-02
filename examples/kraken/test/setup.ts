/**
 * Vitest global setup: `truewire mock` for this project on free ports, its HTTP base URL
 * and WebSocket URL handed to every test through `inject('httpBaseUrl')` and
 * `inject('wsUrl')` (`@truewire/testing/vitest`).
 *
 * The mock binary is `TRUEWIRE_BIN`, else the nearest `.venv/bin/truewire` above the
 * project (the repository's own environment).
 */
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockSetup } from '@truewire/testing/vitest'

declare module 'vitest' {
  export interface ProvidedContext {
    httpBaseUrl: string
    /** The mock's WebSocket URL; `''` when the project records no WebSocket example. */
    wsUrl: string
  }
}

export const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

export default mockSetup({ project: projectRoot })
