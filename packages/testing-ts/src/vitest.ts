/**
 * A vitest `globalSetup` over `startMock`: the mock runs once for the whole test run and
 * every test reads its URLs with `inject('httpBaseUrl')` and `inject('wsUrl')`.
 *
 * ```ts
 * // test/setup.ts
 * import { mockSetup } from '@truewire/testing/vitest'
 *
 * declare module 'vitest' {
 *   export interface ProvidedContext {
 *     httpBaseUrl: string
 *     wsUrl: string
 *   }
 * }
 *
 * export default mockSetup({ project: projectRoot })
 * ```
 *
 * Nothing here imports vitest (a copied package can carry a second copy of it), so the
 * project declares the `ProvidedContext` keys beside its setup, as above.
 */
import { startMock, type MockOptions } from './mock.js'

/** The part of vitest's `TestProject` a global setup uses. */
export interface ProvidingProject {
  provide(key: 'httpBaseUrl' | 'wsUrl', value: string): void
}

/**
 * A `globalSetup` default export that starts the mock, provides `httpBaseUrl` and `wsUrl`
 * (`''` when the project records no WebSocket example), and stops the mock after the run.
 */
export function mockSetup(options: MockOptions): (project: ProvidingProject) => Promise<() => Promise<void>> {
  return async project => {
    const mock = await startMock(options)
    project.provide('httpBaseUrl', mock.httpBaseUrl)
    project.provide('wsUrl', mock.wsUrl ?? '')
    return () => mock.close()
  }
}
