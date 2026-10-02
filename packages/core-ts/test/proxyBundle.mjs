// Exercise the emitted package through server and browser bundlers, including the
// native optional-peer import. Source-level tests cannot catch webpack's empty context.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, rm, symlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import webpack from 'webpack'

const root = fileURLToPath(new URL('..', import.meta.url))
const scratch = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR ?? tmpdir(), 'proxy-bundle-'))
const seen = []
const sockets = new Set()
const proxy = createServer(socket => {
  sockets.add(socket)
  socket.on('close', () => sockets.delete(socket))
  socket.on('error', () => socket.destroy())
  let head = ''
  let upgraded = false
  socket.on('data', chunk => {
    if (upgraded) { socket.end(Buffer.from([0x88, 0])); return }
    head += chunk.toString('latin1')
    if (!head.endsWith('\r\n\r\n')) return
    const line = head.split('\r\n')[0]
    if (line.startsWith('CONNECT ')) {
      seen.push(line)
      socket.write('HTTP/1.1 200 Connection established\r\n\r\n')
    } else if (head.toLowerCase().includes('upgrade: websocket')) {
      const key = /sec-websocket-key: (.+)\r\n/i.exec(head)[1]
      const accept = createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64')
      socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`)
      upgraded = true
    } else {
      seen.push(line)
      socket.end('HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello')
    }
    head = ''
  })
})

try {
  await symlink(join(root, 'node_modules'), join(scratch, 'node_modules'), 'dir')
  const entry = join(scratch, 'entry.mjs')
  await writeFile(entry, `
    import { HttpClient } from ${JSON.stringify(join(root, 'dist/http.js'))}
    import { Socket } from ${JSON.stringify(join(root, 'dist/ws/socket.js'))}
    export async function exercise(proxy) {
      const reply = await new HttpClient({ proxy }).request('GET', 'http://upstream.example.invalid/hello')
      class Bare extends Socket { onMsg() {} }
      const socket = new Bare({ proxy, url: 'ws://upstream.example.invalid/ws' })
      try { await socket.open() } finally { await socket.close() }
      return reply.text()
    }
  `)
  for (const target of ['node', 'web']) {
    await new Promise((resolve, reject) => {
      const compiler = webpack({
        mode: 'production', target, entry,
        experiments: { outputModule: true },
        output: { path: scratch, filename: `${target}.mjs`, library: { type: 'module' } },
        optimization: { minimize: false },
      })
      compiler.run((error, stats) => {
        compiler.close(closeError => {
          if (error || closeError) return reject(error ?? closeError)
          if (stats.hasErrors() || stats.hasWarnings()) return reject(new Error(stats.toString()))
          resolve()
        })
      })
    })
  }
  const browser = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'browser', write: false, metafile: true })
  assert.ok(Object.keys(browser.metafile.inputs).every(path => !path.includes('node_modules/undici/')))
  await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve))
  const { exercise } = await import(pathToFileURL(join(scratch, 'node.mjs')).href)
  assert.equal(await exercise(`http://127.0.0.1:${proxy.address().port}`), 'hello')
  assert.deepEqual(seen, [
    'GET http://upstream.example.invalid/hello HTTP/1.1',
    'CONNECT upstream.example.invalid:80 HTTP/1.1',
  ])
  console.log('webpack server: HTTP + WebSocket through proxy; webpack/esbuild browser: optional peer excluded')
} finally {
  for (const socket of sockets) socket.destroy()
  await new Promise(resolve => proxy.close(resolve))
  await rm(scratch, { recursive: true, force: true })
}
