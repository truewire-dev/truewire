import { inspect } from 'node:util'
import { describe, expect, it } from 'vitest'
import { LogicError } from '../src/errors.js'
import { HttpClient } from '../src/http.js'
import { Socket } from '../src/ws/socket.js'

class Bare extends Socket {
  onMsg(): void {}
}

describe('proxy credentials stay out of what a user logs', () => {
  // Node's invalid-URL `cause` is Node's ERR_INVALID_URL TypeError, whose `input` is the whole URL.
  it.each(['http://alice:s3cret@proxy.corp:99999', 'http://alice:s3cret@proxy corp:3128'])(
    'a malformed proxy URL: console.error(err) does not print the password (%s)', proxy => {
      let error: unknown
      for (const build of [() => new HttpClient({ proxy }), () => new Bare({ url: 'ws://localhost/', proxy })]) {
        try { build() } catch (e) { error = e }
        expect(error).toBeInstanceOf(LogicError)
        expect(inspect(error)).not.toContain('s3cret')
      }
    })

  // Without a scheme, the parsed "protocol" may actually be the username.
  it('a URL without a scheme: the message does not name the username', () => {
    expect(() => new HttpClient({ proxy: 'alice:s3cret@proxy.corp:3128' })).toThrow(LogicError)
    expect(() => new HttpClient({ proxy: 'alice:s3cret@proxy.corp:3128' })).not.toThrow(/alice/)
  })

  // Object inspection must see only the redacted copy, never the transport URL.
  it('console.log(client) and console.log(socket) do not print the password', () => {
    const proxy = 'http://alice:s3cret@127.0.0.1:9'
    for (const client of [new HttpClient({ proxy }), new Bare({ url: 'ws://127.0.0.1:9/', proxy })]) {
      expect(inspect(client)).not.toContain('s3cret')
      expect(inspect(client)).not.toContain('alice')
      expect(client.proxy).toBe('http://127.0.0.1:9/')
    }
  })
})
