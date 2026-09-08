/**
 * Vitest global setup: start `truewire mock` for this project on free ports and hand its
 * HTTP base URL and WebSocket URL to every test through `inject('httpBaseUrl')` and
 * `inject('wsUrl')`.
 *
 * The mock binary is `../../.venv/bin/truewire` (the repository's own environment) unless
 * `TRUEWIRE_BIN` names another one.
 */
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { TestProject } from 'vitest/node'

declare module 'vitest' {
  export interface ProvidedContext {
    httpBaseUrl: string
    wsUrl: string
  }
}

export const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

export default async function setup(project: TestProject): Promise<() => void> {
  const bin = process.env.TRUEWIRE_BIN ?? path.resolve(projectRoot, '../../.venv/bin/truewire')
  const child = spawn(bin, ['mock', '--project', projectRoot, '--http-port', '0', '--ws-port', '0'], {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  })
  const stderr: string[] = []
  child.stderr.on('data', chunk => { stderr.push(String(chunk)) })
  const urls = await new Promise<{ httpBaseUrl: string; wsUrl: string }>((resolve, reject) => {
    const found: { httpBaseUrl?: string; wsUrl?: string } = {}
    const lines = createInterface({ input: child.stdout })
    lines.on('line', line => {
      const match = /^(HTTP|WS)\s+(\S+)/.exec(line)
      if (match?.[1] === 'HTTP') found.httpBaseUrl = match[2]
      if (match?.[1] === 'WS') found.wsUrl = match[2]
      if (found.httpBaseUrl && found.wsUrl) resolve({ httpBaseUrl: found.httpBaseUrl, wsUrl: found.wsUrl })
    })
    child.on('exit', code => reject(new Error(`truewire mock exited with ${code}: ${stderr.join('')}`)))
    child.on('error', reject)
  })
  project.provide('httpBaseUrl', urls.httpBaseUrl)
  project.provide('wsUrl', urls.wsUrl)
  return () => { child.kill() }
}
