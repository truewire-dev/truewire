/**
 * `truewire mock` as a child process a foreign test runner can own.
 *
 * `startMock` spawns `truewire mock --project <root> --http-port 0 --ws-port 0 --json`,
 * reads the JSON ready line it prints once it is listening (`{"event": "ready", "http":
 * <url>, "ws": <url> | null}`), and hands back a handle that stops the process. The text
 * lines an older `truewire` prints (`HTTP  <url>`, then `WS    <url>` or `WS    (no
 * websocket examples in this project)`) are still understood.
 */
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { createInterface } from 'node:readline'

export interface MockOptions {
  /** The project root: the directory holding `truewire.toml`. */
  project: string
  /**
   * The `truewire` executable. Defaults to `TRUEWIRE_BIN`, else the nearest
   * `.venv/bin/truewire` at or above `project`, else `truewire` on `PATH`.
   */
  bin?: string
  /** HTTP port; `0` (the default) picks a free one. */
  httpPort?: number
  /** WebSocket port; `0` (the default) picks a free one. */
  wsPort?: number
  /** Milliseconds to wait for the URLs before giving up; 30 seconds by default. */
  timeout?: number
  /** Extra environment for the child process. */
  env?: Record<string, string>
}

/** A running mock. `close()` (or `await using`) stops it. */
export interface MockServer extends AsyncDisposable {
  /** Base URL of the HTTP server, no trailing slash. */
  readonly httpBaseUrl: string
  /** URL of the WebSocket server; `undefined` when the project records no WebSocket example. */
  readonly wsUrl: string | undefined
  readonly process: ChildProcess
  close(): Promise<void>
}

/** The `truewire` executable `startMock` runs when `bin` is not given. */
export function truewireBin(project: string): string {
  const fromEnv = process.env.TRUEWIRE_BIN
  if (fromEnv) return fromEnv
  let dir = path.resolve(project)
  for (;;) {
    const candidate = path.join(dir, '.venv', 'bin', 'truewire')
    if (existsSync(candidate)) return candidate
    const parent = path.dirname(dir)
    if (parent === dir) return 'truewire'
    dir = parent
  }
}

/**
 * Parse one line `truewire mock` prints on start-up: the `--json` ready object (both URLs
 * at once), or one of the text lines.
 */
export function parseReadyLine(line: string): { http?: string; ws?: string | null } | null {
  if (line.startsWith('{')) {
    let ready: unknown
    try {
      ready = JSON.parse(line)
    } catch {
      return null
    }
    if (typeof ready !== 'object' || ready === null || (ready as { event?: unknown }).event !== 'ready') return null
    const { http, ws } = ready as { http?: unknown; ws?: unknown }
    if (typeof http !== 'string') return null
    return { http: http.replace(/\/$/, ''), ws: typeof ws === 'string' ? ws : null }
  }
  const match = /^(HTTP|WS)\s+(\S+)/.exec(line)
  if (match === null) return null
  const value = match[2]!
  if (match[1] === 'HTTP') return { http: value.replace(/\/$/, '') }
  return { ws: value.startsWith('(') ? null : value }
}

/** Start `truewire mock` for `options.project` and wait until it prints its URLs. */
export async function startMock(options: MockOptions): Promise<MockServer> {
  const bin = options.bin ?? truewireBin(options.project)
  const args = [
    'mock', '--project', path.resolve(options.project),
    '--http-port', String(options.httpPort ?? 0), '--ws-port', String(options.wsPort ?? 0), '--json',
  ]
  const child = spawn(bin, args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONUNBUFFERED: '1', ...options.env },
  })
  const stderr: string[] = []
  child.stderr!.on('data', chunk => { stderr.push(String(chunk)) })
  const exited = new Promise<void>(resolve => { child.once('exit', () => resolve()) })

  const close = async () => {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill()
      await exited
    }
  }

  let timer: NodeJS.Timeout | undefined
  try {
    const urls = await new Promise<{ http: string; ws: string | undefined }>((resolve, reject) => {
      let http: string | undefined
      const lines = createInterface({ input: child.stdout! })
      lines.on('line', line => {
        const parsed = parseReadyLine(line)
        if (parsed?.http !== undefined) http = parsed.http
        if (parsed?.ws !== undefined && http !== undefined) resolve({ http, ws: parsed.ws ?? undefined })
      })
      child.once('exit', code => reject(new Error(`truewire mock exited with ${code}: ${stderr.join('')}`)))
      child.once('error', error => reject(new Error(`could not start ${bin}: ${error.message}`, { cause: error })))
      const timeout = options.timeout ?? 30_000
      timer = setTimeout(() => reject(new Error(`truewire mock printed no URLs within ${timeout} ms: ${stderr.join('')}`)), timeout)
    })
    return {
      httpBaseUrl: urls.http,
      wsUrl: urls.ws,
      process: child,
      close,
      [Symbol.asyncDispose]: close,
    }
  } catch (error) {
    await close()
    throw error
  } finally {
    clearTimeout(timer)
  }
}
