/**
 * The exception hierarchy a generated client's callers program against.
 *
 * Every error is a `TruewireError`; API-returned errors are `ApiError`s (with
 * `BadRequest`/`AuthError`/`RateLimited` beneath), and transport/format/SDK failures are
 * siblings, never `ApiError`s. Each class carries a string-literal `code`, so a caller can
 * duck-type an error that crossed a duplicate-bundle boundary (`err.code === 'auth'`)
 * where `instanceof` would fail.
 */

export type ErrorCode =
  | 'error'
  | 'network'
  | 'validation'
  | 'api'
  | 'bad-request'
  | 'auth'
  | 'rate-limited'
  | 'logic'

/** Base error. */
export class TruewireError extends Error {
  override readonly name: string = 'TruewireError'
  readonly code: ErrorCode = 'error'

  constructor(message = '', options?: ErrorOptions) {
    super(message, options)
  }
}

/** Error reaching the server. */
export class NetworkError extends TruewireError {
  override readonly name: string = 'NetworkError'
  override readonly code: ErrorCode = 'network'
}

/** One failed check inside a value, located by a JSON pointer. */
export interface Issue {
  /** JSON pointer to the offending value, `''` for the root. */
  path: string
  message: string
}

/** Invalid response format. */
export class ValidationError extends TruewireError {
  override readonly name: string = 'ValidationError'
  override readonly code: ErrorCode = 'validation'
  /** JSON pointer to the first offending value, `''` for the root. */
  readonly path: string
  /** Every check that failed, in order. */
  readonly issues: readonly Issue[]

  constructor(message = '', options?: ErrorOptions & { issues?: readonly Issue[] }) {
    super(message, options)
    this.issues = options?.issues ?? [{ path: '', message }]
    this.path = this.issues[0]?.path ?? ''
  }
}

export interface ApiErrorOptions extends ErrorOptions {
  /** HTTP status of the reply that carried the error, when there was one. */
  status?: number
  /** Decoded body of the reply that carried the error, when there was one. */
  body?: unknown
}

/** Error returned by the API. */
export class ApiError extends TruewireError {
  override readonly name: string = 'ApiError'
  override readonly code: ErrorCode = 'api'
  readonly status: number | undefined
  readonly body: unknown

  constructor(message = '', options?: ApiErrorOptions) {
    super(message, options)
    this.status = options?.status
    this.body = options?.body
  }
}

/** Bad request: invalid request, invalid input, etc. */
export class BadRequest extends ApiError {
  override readonly name: string = 'BadRequest'
  override readonly code: ErrorCode = 'bad-request'
}

/** Authentication error: invalid API key, invalid API secret, etc. */
export class AuthError extends ApiError {
  override readonly name: string = 'AuthError'
  override readonly code: ErrorCode = 'auth'
}

/** Rate limited: the API has reached the rate limit. */
export class RateLimited extends ApiError {
  override readonly name: string = 'RateLimited'
  override readonly code: ErrorCode = 'rate-limited'
}

/** Logic error: invalid assumptions, logic, or other bugs on the SDK side. */
export class LogicError extends TruewireError {
  override readonly name: string = 'LogicError'
  override readonly code: ErrorCode = 'logic'
}

/** Whether `value` is a `TruewireError`, by `instanceof` or by its `code` when bundles are duplicated. */
export function isTruewireError(value: unknown): value is TruewireError {
  return value instanceof TruewireError
    || (value instanceof Error && typeof (value as { code?: unknown }).code === 'string'
      && (value as { name: string }).name in KNOWN)
}

const KNOWN: Record<string, ErrorCode> = {
  TruewireError: 'error', NetworkError: 'network', ValidationError: 'validation', ApiError: 'api',
  BadRequest: 'bad-request', AuthError: 'auth', RateLimited: 'rate-limited', LogicError: 'logic',
}
