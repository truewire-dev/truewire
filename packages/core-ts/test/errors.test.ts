/**
 * Pins the exception hierarchy a generated client's callers program against: every error is
 * a `TruewireError`, API-returned errors are `ApiError`s (with `BadRequest`/`AuthError`/
 * `RateLimited` beneath), and transport/format/SDK failures are siblings, never `ApiError`s.
 */
import { describe, expect, it } from 'vitest'
import {
  ApiError, AuthError, BadRequest, LogicError, NetworkError, RateLimited, TruewireError, ValidationError,
  isTruewireError,
} from '../src/errors.js'

const leaves = [NetworkError, ValidationError, ApiError, BadRequest, AuthError, RateLimited, LogicError]

describe('error hierarchy', () => {
  it.each(leaves)('%o is a TruewireError and an Error', cls => {
    const err = new cls('x')
    expect(err).toBeInstanceOf(TruewireError)
    expect(err).toBeInstanceOf(Error)
  })

  it.each([BadRequest, AuthError, RateLimited])('%o is an ApiError', cls => {
    expect(new cls('x')).toBeInstanceOf(ApiError)
  })

  it.each([NetworkError, ValidationError, LogicError])('%o is not an ApiError', cls => {
    // A caller catching `ApiError` must not swallow a dropped connection or an SDK bug.
    expect(new cls('x')).not.toBeInstanceOf(ApiError)
  })

  it('ApiError leaves are siblings', () => {
    expect(new AuthError('x')).not.toBeInstanceOf(BadRequest)
    expect(new RateLimited('x')).not.toBeInstanceOf(BadRequest)
    expect(new RateLimited('x')).not.toBeInstanceOf(AuthError)
  })

  it('catching TruewireError catches everything', () => {
    for (const cls of leaves) {
      expect(() => { throw new cls('boom') }).toThrow(TruewireError)
    }
  })

  it('renders as the concrete class name and the message', () => {
    expect(String(new AuthError('bad key'))).toBe('AuthError: bad key')
  })

  it('keeps a cause', () => {
    const cause = new Error('timeout')
    const err = new NetworkError('GET /x', { cause })
    expect(err.cause).toBe(cause)
    expect(String(err)).toBe('NetworkError: GET /x')
  })

  it('renders with no message', () => {
    expect(String(new LogicError())).toBe('LogicError')
  })

  it('names the concrete class', () => {
    // `name` is set per class, so a log line says which one.
    expect(new RateLimited('slow down').name).toBe('RateLimited')
    expect(String(new RateLimited('slow down')).startsWith('RateLimited')).toBe(true)
  })

  it('carries a string code per class for duck typing across bundles', () => {
    expect(new AuthError().code).toBe('auth')
    expect(new RateLimited().code).toBe('rate-limited')
    expect(new ValidationError().code).toBe('validation')
    const foreign = Object.assign(new Error('x'), { code: 'network', name: 'NetworkError' })
    expect(isTruewireError(foreign)).toBe(true)
    expect(isTruewireError(new Error('x'))).toBe(false)
    expect(isTruewireError(new LogicError())).toBe(true)
  })

  it('ValidationError carries its path and issues', () => {
    const err = new ValidationError('expected string at /a/0', { issues: [{ path: '/a/0', message: 'expected string' }] })
    expect(err.path).toBe('/a/0')
    expect(err.issues).toHaveLength(1)
    expect(new ValidationError('root').path).toBe('')
  })

  it('ApiError carries the status and body of the reply', () => {
    const err = new RateLimited('slow down', { status: 429, body: { error: 'EGeneral:Too many requests' } })
    expect(err.status).toBe(429)
    expect(err.body).toEqual({ error: 'EGeneral:Too many requests' })
  })
})
