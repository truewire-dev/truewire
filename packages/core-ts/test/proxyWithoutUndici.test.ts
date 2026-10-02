/**
 * `proxy` without the optional peer `undici` fails with a `LogicError` that says how to
 * install it, on both transports: never a silent direct connection, never a `NetworkError`
 * that reads like the proxy being down. A file of its own, since the mock applies to the
 * whole module graph.
 */
import { describe, expect, it, vi } from 'vitest'
import { LogicError } from '../src/errors.js'
import { HttpClient } from '../src/http.js'
import { Socket } from '../src/ws/socket.js'

vi.mock('undici', () => { throw new Error("Cannot find package 'undici'") })

class Bare extends Socket {
  onMsg(): void {}
}

describe('proxy without undici installed', () => {
  it('fails the first request and the first connection with how to fix it', async () => {
    const proxy = 'http://127.0.0.1:9'
    const request = new HttpClient({ proxy }).request('GET', 'http://127.0.0.1:9/')
    await expect(request).rejects.toBeInstanceOf(LogicError)
    await expect(request).rejects.toThrow('`proxy` needs the optional peer dependency `undici` (npm install undici)')
    const open = new Bare({ url: 'ws://127.0.0.1:9/', proxy }).open()
    await expect(open).rejects.toBeInstanceOf(LogicError)
    await expect(open).rejects.toThrow('npm install undici')
  })
})
