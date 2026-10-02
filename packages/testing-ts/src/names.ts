/**
 * The naming rule `truewire generate typescript` applies to every function-path segment,
 * so a dotted function path (`trading_ws.add_order`) resolves to the generated member
 * chain (`client.tradingWs.addOrder`) and the endpoint module file (`trading_ws/add_order.ts`).
 */

/** `list_commits` -> `listCommits`; `get` -> `get`; `getRepo` -> `getRepo`; `market-data` -> `marketData`. */
export function camelCase(segment: string): string {
  const parts = segment.replace(/-/g, '_').split('_').filter(Boolean)
  if (parts.length === 0) return segment
  const [head, ...rest] = parts
  return head!.slice(0, 1).toLowerCase() + head!.slice(1) + rest.map(part => part.slice(0, 1).toUpperCase() + part.slice(1)).join('')
}

/** A method of the generated client, bound to its router. */
export type Method = (...args: unknown[]) => unknown

/**
 * The bound generated method a dotted function path names on `client`, or `undefined`
 * when the client has no such router or method.
 */
export function resolveMethod(client: unknown, fn: string): Method | undefined {
  const segments = fn.split('.')
  let target: unknown = client
  for (const segment of segments.slice(0, -1)) {
    if (target === null || typeof target !== 'object') return undefined
    target = (target as Record<string, unknown>)[camelCase(segment)]
  }
  if (target === null || typeof target !== 'object') return undefined
  const method = (target as Record<string, unknown>)[camelCase(segments.at(-1)!)]
  return typeof method === 'function' ? (method as Method).bind(target) : undefined
}

/** The generated endpoint module of a function path, under the package directory: `<packageDir>/a/b.ts`. */
export function endpointModulePath(packageDir: string, fn: string): string {
  return [packageDir.replace(/\/$/, ''), ...fn.split('.')].join('/') + '.ts'
}
