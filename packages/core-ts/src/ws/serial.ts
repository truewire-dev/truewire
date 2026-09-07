/** Request/reply for a connection whose replies carry no correlation id. */
import { AsyncQueue, Lock } from './async.js'

export interface SerialOptions<Reply> {
  /** Write one message to the wire. */
  send: (msg: unknown) => Promise<void>
  /** Race a reply against the connection's fate, typically the owning socket's `wait`. */
  wait?: (reply: Promise<Reply>) => Promise<Reply>
}

/**
 * Serializes each request under a lock and matches it to the very next reply, by arrival
 * order alone -- the only correlation an API like this gives you.
 *
 * Compose it beside a `Streams`, `Rpc` or `StreamsRpc` (whichever owns the connection),
 * so a project can mix correlation strategies on one connection: route a parsed reply
 * into `replies.push(...)` from `parseMsg`/`parseResponse` and return `null` there; this
 * class never parses incoming frames itself, it only fulfils replies routed to it.
 *
 * ```ts
 * readonly serial = new SerialReplies<Reply>({ send: m => this.send(m), wait: p => this.wait(p) })
 * ```
 *
 * Many concurrent `request()` calls are fine; they are serialized internally.
 */
export class SerialReplies<Reply = unknown> {
  readonly replies = new AsyncQueue<Reply>()
  readonly lock = new Lock()
  readonly #send: (msg: unknown) => Promise<void>
  readonly #wait: (reply: Promise<Reply>) => Promise<Reply>

  constructor(options: SerialOptions<Reply>) {
    this.#send = options.send
    this.#wait = options.wait ?? (p => p)
  }

  /** Send `msg` and return the next reply, serialized against concurrent callers. */
  request(msg: unknown): Promise<Reply> {
    return this.lock.run(async () => {
      await this.#send(msg)
      return this.#wait(this.replies.pull())
    })
  }
}
