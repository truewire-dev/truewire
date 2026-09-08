/**
 * Kraken Spot REST's `{error, result}` envelope, and its errors mapped onto the
 * `@truewire/core` hierarchy by Kraken's own error categories.
 *
 * @see https://docs.kraken.com/api/docs/guides/global-errors
 */
import { ApiError, AuthError, BadRequest, RateLimited, ValidationError } from '@truewire/core'

/** The `ApiError` subclass per `<Category>:<Description>` prefix, unless a substring below decides first. */
const CATEGORY_ERRORS: Record<string, typeof ApiError> = {
  EAPI: AuthError,
  EAuth: AuthError,
  EAccount: AuthError,
  EGeneral: BadRequest,
  ETrade: BadRequest,
  EFunding: BadRequest,
}

const RATE_LIMIT_SUBSTRINGS = [
  'Rate limit exceeded', 'Too many requests', 'Orders limit exceeded', 'Domain rate limit exceeded',
  'Scheduled orders limit exceeded',
]
const AUTH_SUBSTRINGS = ['Invalid key', 'Invalid signature', 'Invalid nonce', 'Permission denied', 'Temporary lockout']
const BAD_REQUEST_SUBSTRINGS = ['Invalid price', 'Tick size check failed', 'Order minimum not met', 'Cost minimum not met']

/** Throw the error a non-empty `error` array calls for; the first entry decides, as Kraken lists the primary failure first. */
export function raiseError(errors: string[]): never {
  const message = errors[0] ?? 'unknown error'
  const category = message.split(':', 1)[0] ?? ''
  const options = { body: errors }
  if (RATE_LIMIT_SUBSTRINGS.some(s => message.includes(s))) throw new RateLimited(message, options)
  if (AUTH_SUBSTRINGS.some(s => message.includes(s))) throw new AuthError(message, options)
  if (BAD_REQUEST_SUBSTRINGS.some(s => message.includes(s))) throw new BadRequest(message, options)
  throw new (CATEGORY_ERRORS[category] ?? ApiError)(message, options)
}

/** A non-2xx status, rare for Kraken (it answers 200 to most logical errors) but what an edge failure looks like. */
export function raiseHttpStatus(status: number, text: string): never {
  let body: unknown = text
  try { body = JSON.parse(text) } catch { /* not JSON: keep the text */ }
  const message = `HTTP ${status}: ${text.slice(0, 200)}`
  const options = { status, body }
  if (status === 401 || status === 403) throw new AuthError(message, options)
  if (status === 429) throw new RateLimited(message, options)
  if (status >= 400 && status < 500) throw new BadRequest(message, options)
  throw new ApiError(message, options)
}

/** The envelope's `result`, or the error its `error` array or status calls for. */
export function unwrap(status: number, text: string): unknown {
  if (status >= 400) raiseHttpStatus(status, text)
  let envelope: unknown
  try { envelope = JSON.parse(text) } catch (e) {
    throw new ValidationError(`invalid JSON: ${(e as Error).message}`, { cause: e })
  }
  if (typeof envelope !== 'object' || envelope === null || !Array.isArray((envelope as { error?: unknown }).error)) {
    throw new ValidationError('not a Kraken envelope: no `error` array', { issues: [{ path: '/error', message: 'expected an array' }] })
  }
  const { error, result } = envelope as { error: string[]; result?: unknown }
  if (error.length > 0) raiseError(error)
  return result
}
