/**
 * Packages clause P18 through the hand-written core: `proxy` reaches the HTTP transport.
 * That the runtime then sends through it is `@truewire/core`'s own test
 * (`test/proxy.test.ts`).
 */
import { HttpClient, LogicError } from '@truewire/core'
import { describe, expect, it } from 'vitest'
import { Core } from '../src/github/core/index.js'

const PROXY = 'http://proxy.example:3128'

describe('Core({ proxy })', () => {
  it('hands the proxy to the HTTP transport', () => {
    expect(new Core({ proxy: PROXY }).http.proxy).toBe(PROXY)
    expect(new Core().http.proxy).toBeUndefined()
  })

  it('refuses a proxy beside a ready-made HTTP client, which would ignore it', () => {
    expect(() => new Core({ proxy: PROXY, http: new HttpClient() })).toThrow(LogicError)
  })
})
