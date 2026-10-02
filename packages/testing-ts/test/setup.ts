/** Vitest global setup: `truewire mock` over the `pets` fixture, through `@truewire/testing` itself. */
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockSetup } from '../src/vitest.js'

declare module 'vitest' {
  export interface ProvidedContext {
    httpBaseUrl: string
    /** The mock's WebSocket URL; `''` when the project records no WebSocket example. */
    wsUrl: string
  }
}

export const fixtureRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), 'fixture')
export const packageDir = path.join(fixtureRoot, 'src', 'pets')

export default mockSetup({ project: fixtureRoot })
