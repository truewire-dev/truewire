// `truewire call typescript`: one call, or one subscription, through a generated TypeScript client.
//
// Run by `truewire.call.native` as
//   node --experimental-transform-types --no-warnings call.mjs
// with the job as one line of JSON on stdin (never argv, where `ps` shows the secrets in
// `options`), and stdin left open: its end means the CLI is gone, however it died, and the
// driver exits at once rather than hold a subscription nobody reads.
// { packageDir, root, function, kind, arguments, options }. The client is
// `new <root>(new Core(options))`, `Core` from `<packageDir>/core/index.ts`; the method the
// function path names is called with `arguments` parsed through the endpoint module's
// `Request` (a stream's `Parameters`) codec, so it is typed and validated as a caller's is.
//
// Every decoded value goes back to wire form through the codec the generated method handed
// its core; with validation off for the call (its own `validate`, else the core's), the core
// returns the raw JSON, which is printed as it is. Each goes out on stdout as one JSON line:
// {"result"}, or {"reply"}, {"message"}..., {"unsubscribed"}; a failure is
// {"error": {"type", "message"}} and exit 1. The first SIGINT or SIGTERM unsubscribes; the
// second exits at once.
import { registerHooks } from 'node:module'
import { existsSync, writeSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'

// The generated sources import `./x.js` for a file on disk as `./x.ts`.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.endsWith('.js') && (specifier.startsWith('.') || specifier.startsWith('file:')) && context.parentURL?.startsWith('file:')) {
      const url = new URL(specifier, context.parentURL)
      if (!existsSync(fileURLToPath(url))) {
        const ts = new URL(url.href.slice(0, -'.js'.length) + '.ts')
        if (existsSync(fileURLToPath(ts))) return nextResolve(ts.href, context)
      }
    }
    return nextResolve(specifier, context)
  },
})

const UNSUBSCRIBE_TIMEOUT_MS = 10_000

// Integers past 2^53 keep their digits both ways, as `@truewire/core`'s lossless parse
// keeps them: an integer literal a `number` cannot hold exactly comes in as a `bigint`,
// and a `bigint` goes out as its digits.
const job = await readJob().catch(error => fail(error))

/** The first line on stdin, parsed; the end of stdin, then or later, exits the driver. */
function readJob() {
  return new Promise((resolve, reject) => {
    let text = ''
    let read = false
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', chunk => {
      if (read) return
      text += chunk
      const end = text.indexOf('\n')
      if (end < 0) return
      read = true
      try {
        resolve(JSON.parse(text.slice(0, end), (_, value, { source } = {}) =>
          typeof value === 'number' && !Number.isSafeInteger(value) && /^-?\d+$/.test(source ?? '') ? BigInt(source) : value))
      } catch (error) {
        reject(error)
      }
    })
    process.stdin.on('end', () => {
      if (read) process.exit(1)
      reject(new Error('the driver reads its job as one line of JSON on stdin'))
    })
  })
}

function emit(key, value) {
  const line = JSON.stringify({ [key]: value }, (_, v) => (typeof v === 'bigint' ? JSON.rawJSON(String(v)) : v))
  writeSync(1, line + '\n')
}

function fail(error, code = 1) {
  const type = error?.name ?? 'Error'
  const message = error instanceof Error ? error.message : String(error)
  emit('error', { type, message })
  process.exit(code)
}

function camelCase(segment) {
  const parts = segment.replace(/-/g, '_').split('_').filter(Boolean)
  if (parts.length === 0) return segment
  const [head, ...rest] = parts
  return head.slice(0, 1).toLowerCase() + head.slice(1) + rest.map(p => p.slice(0, 1).toUpperCase() + p.slice(1)).join('')
}

function resolveMethod(client, fn) {
  const segments = fn.split('.')
  let target = client
  for (const segment of segments.slice(0, -1)) {
    if (target === null || typeof target !== 'object') return undefined
    target = target[camelCase(segment)]
  }
  if (target === null || typeof target !== 'object') return undefined
  const method = target[camelCase(segments.at(-1))]
  return typeof method === 'function' ? method.bind(target) : undefined
}

/**
 * `core`, with every method call that hands over a codec (`request`, `command`,
 * `subscribe`, ...) recorded in `seen`: the call as `seen.call` and the object it went to
 * as `seen.core`, so the value it returns can be dumped back to the wire. Calls run on the
 * real object, so private fields and `this` behave as without it.
 */
function recording(core, seen) {
  const proxies = new WeakMap()
  const wrap = target => {
    if (target === null || typeof target !== 'object') return target
    if (!proxies.has(target)) {
      proxies.set(target, new Proxy(target, {
        get(object, key) {
          const value = Reflect.get(object, key, object)
          if (typeof value === 'function') {
            return (...args) => {
              const call = args[0]
              if (call !== null && typeof call === 'object' && ('responseCodec' in call || 'messageCodec' in call)) {
                seen.call = call
                seen.core = object
              }
              return value.apply(object, args)
            }
          }
          return wrap(value)
        },
      }))
    }
    return proxies.get(target)
  }
  return wrap(core)
}

/**
 * Whether the core decoded the value the recorded call returned, as the contract has a core
 * decide it: the call's own `validate`, else the core's `validate`, else the `validate` the
 * core was built with. A core that states none validates, the contract's default.
 */
function validated(seen) {
  for (const value of [seen.call?.validate, seen.core?.validate, job.options.validate]) {
    if (typeof value === 'boolean') return value
  }
  return true
}

/** `value` in wire form: dumped through `codec` when the core decoded it, else the raw JSON it returned. */
function wire(codec, value, seen) {
  if (value === undefined) return null
  return codec && value !== null && validated(seen) ? codec.dump(value) : value
}

async function main() {
  const packageUrl = pathToFileURL(job.packageDir + '/').href
  const endpointUrl = new URL(job.function.split('.').join('/') + '.ts', packageUrl)
  const [index, coreModule, endpoint] = await Promise.all([
    import(new URL('index.ts', packageUrl).href),
    import(new URL('core/index.ts', packageUrl).href),
    // A method with no module of its own (a hand-written extra) takes its value as it is.
    existsSync(fileURLToPath(endpointUrl)) ? import(endpointUrl.href) : {},
  ])
  const Root = index[job.root]
  if (typeof Root !== 'function') throw new Error(`${job.packageDir}/index.ts exports no ${job.root}`)
  if (typeof coreModule.Core !== 'function') throw new Error(`${job.packageDir}/core/index.ts exports no Core; call builds new ${job.root}(new Core(options))`)
  const seen = {}
  const core = new coreModule.Core(job.options)
  const client = new Root(recording(core, seen))
  const method = resolveMethod(client, job.function)
  if (method === undefined) throw new Error(`${job.function}: the generated client has no such method; run \`truewire generate typescript\``)
  const codec = endpoint[job.kind === 'stream' ? 'Parameters' : 'Request']
  const args = codec && typeof codec.parse === 'function'
    ? [codec.parse(job.arguments)]
    : Object.keys(job.arguments).length === 0 ? [] : [job.arguments]
  try {
    if (job.kind === 'stream') await stream(method(...args), seen)
    else {
      // `seen` holds the codec only once the method has handed it to the core.
      const result = await method(...args)
      emit('result', wire(seen.call?.responseCodec, result, seen))
    }
  } finally {
    await (core.close?.() ?? core[Symbol.asyncDispose]?.())
  }
}

async function stream(subscription, seen) {
  let stopped
  const stop = new Promise(resolve => { stopped = resolve })
  const onSignal = () => {
    process.off('SIGINT', onSignal)
    process.off('SIGTERM', onSignal)
    process.on('SIGINT', () => process.exit(130))
    process.on('SIGTERM', () => process.exit(130))
    stopped('stop')
  }
  process.on('SIGINT', onSignal)
  process.on('SIGTERM', onSignal)
  const opened = await Promise.race([subscription.open(), stop])
  if (opened === 'stop') return
  emit('reply', wire(seen.call?.replyCodec, opened.reply, seen))
  const iterator = opened[Symbol.asyncIterator]()
  for (;;) {
    const next = await Promise.race([iterator.next(), stop])
    if (next === 'stop') break
    if (next.done) return
    emit('message', wire(seen.call?.messageCodec, next.value, seen))
  }
  let timer
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const error = new Error(`${job.function}: no unsubscribe reply within ${UNSUBSCRIBE_TIMEOUT_MS / 1000} s; closing the connection`)
      error.name = 'CallError'
      reject(error)
    }, UNSUBSCRIBE_TIMEOUT_MS)
  })
  try {
    emit('unsubscribed', wire(undefined, await Promise.race([opened.unsubscribe(), timeout]), seen))
  } finally {
    clearTimeout(timer)
  }
}

main().then(() => process.exit(0), error => fail(error))
