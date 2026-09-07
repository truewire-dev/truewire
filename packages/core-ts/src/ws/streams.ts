/**
 * Multiplexed subscribe/stream client: many concurrent channel subscriptions on one
 * connection, each consumed as an async iterable.
 */
import { LogicError } from '../errors.js'
import { AsyncQueue, Deferred } from './async.js'
import { Socket, type Data } from './socket.js'

/** A parsed channel push: which local channel it belongs to, and what it carried. */
export interface ChannelMessage<Notification> {
  channel: string
  notification: Notification
}

async function* mapIter<A, B>(source: AsyncIterable<A>, f: (a: A) => B): AsyncGenerator<B> {
  for await (const item of source) yield f(item)
}

async function* filterIter<A>(source: AsyncIterable<A>, f: (a: A) => boolean): AsyncGenerator<A> {
  for await (const item of source) if (f(item)) yield item
}

/**
 * A live subscription: the reply that acknowledged it, the notifications as they arrive,
 * and `unsubscribe()`.
 *
 * ```ts
 * console.log(stream.reply)
 * for await (const msg of stream) ...
 * await stream.unsubscribe()
 * ```
 */
export class Stream<Notification, SubscriptionReply = unknown, UnsubscriptionReply = unknown>
  implements AsyncIterable<Notification>, AsyncDisposable {
  constructor(
    readonly reply: SubscriptionReply,
    readonly stream: AsyncIterable<Notification>,
    /** Unsubscribe; the reply, or `undefined` when already unsubscribed. */
    readonly unsubscribe: () => Promise<UnsubscriptionReply | undefined>,
  ) {}

  [Symbol.asyncIterator](): AsyncIterator<Notification> {
    return this.stream[Symbol.asyncIterator]()
  }

  map<T>(f: (notification: Notification) => T): Stream<T, SubscriptionReply, UnsubscriptionReply> {
    return new Stream(this.reply, mapIter(this.stream, f), this.unsubscribe)
  }

  filter(f: (notification: Notification) => boolean): Stream<Notification, SubscriptionReply, UnsubscriptionReply> {
    return new Stream(this.reply, filterIter(this.stream, f), this.unsubscribe)
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.unsubscribe()
  }
}

/**
 * A subscription not yet requested, usable three ways:
 *
 * 1. Awaited, for the `Stream` (subscribes now; unsubscribe by hand):
 *    ```ts
 *    const stream = await subscription
 *    for await (const msg of stream) ...
 *    await stream.unsubscribe()
 *    ```
 * 2. Iterated directly: subscribes on the first pull.
 * 3. Disposed, for automatic cleanup:
 *    ```ts
 *    await using stream = client.streams.ticker({ symbol: 'BTC/USD' })
 *    for await (const msg of stream) ...
 *    // unsubscribed on scope exit
 *    ```
 */
export class Subscription<Notification, SubscriptionReply = unknown, UnsubscriptionReply = unknown>
  implements PromiseLike<Stream<Notification, SubscriptionReply, UnsubscriptionReply>>, AsyncIterable<Notification>, AsyncDisposable {
  /** The stream once `open()`/iteration connected it; `null` before, and after disposal. */
  stream: Stream<Notification, SubscriptionReply, UnsubscriptionReply> | null = null

  constructor(readonly connect: () => Promise<Stream<Notification, SubscriptionReply, UnsubscriptionReply>>) {}

  /** Subscribe once and keep the stream for iteration and disposal. */
  async open(): Promise<Stream<Notification, SubscriptionReply, UnsubscriptionReply>> {
    return (this.stream ??= await this.connect())
  }

  then<R1 = Stream<Notification, SubscriptionReply, UnsubscriptionReply>, R2 = never>(
    onfulfilled?: ((value: Stream<Notification, SubscriptionReply, UnsubscriptionReply>) => R1 | PromiseLike<R1>) | null,
    onrejected?: ((reason: unknown) => R2 | PromiseLike<R2>) | null,
  ): Promise<R1 | R2> {
    return this.connect().then(onfulfilled, onrejected)
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<Notification> {
    yield* await this.open()
  }

  /** Unsubscribe if `open()`/iteration subscribed. */
  async [Symbol.asyncDispose](): Promise<void> {
    const stream = this.stream
    this.stream = null
    if (stream) await stream.unsubscribe()
  }

  map<T>(f: (notification: Notification) => T): Subscription<T, SubscriptionReply, UnsubscriptionReply> {
    return new Subscription(async () => (await this.connect()).map(f))
  }

  filter(f: (notification: Notification) => boolean): Subscription<Notification, SubscriptionReply, UnsubscriptionReply> {
    return new Subscription(async () => (await this.connect()).filter(f))
  }
}

export interface SubscribeOptions<Notification> {
  /** Channel identifier to use for the subscription request, if different from the local one. */
  requestChannel?: string
  /** Derive the local channel identifier from an incoming notification; the channel itself matches otherwise. */
  messageKey?: (notification: Notification) => string
}

/** The pieces of a socket the subscription machinery needs; `Streams` and `StreamsRpc` both provide them. */
export interface SubscriptionHost<Notification, SubscriptionParams, SubscriptionReply, UnsubscriptionReply> extends Socket {
  /** Subscription queues, keyed by the local channel identifier. */
  readonly subscriptions: Map<string, AsyncQueue<Notification>>
  /** Functions to determine the local channel identifier for an incoming notification, keyed by request channel. */
  readonly messageKeys: Map<string, (notification: Notification) => string>
  requestSubscription(channel: string, params?: SubscriptionParams): Promise<SubscriptionReply>
  requestUnsubscription(channel: string, params?: SubscriptionParams): Promise<UnsubscriptionReply>
}

/** Route one parsed channel push to its subscription queue, if any. */
export function dispatch<N>(host: SubscriptionHost<N, unknown, unknown, unknown>, message: ChannelMessage<N>): void {
  let channel = message.channel
  const key = host.messageKeys.get(channel)
  if (key) channel = key(message.notification)
  host.subscriptions.get(channel)?.push(message.notification)
}

/** Subscribe `host` to `channel`: the `Streams.subscribe` body, shared with `StreamsRpc`. */
export async function subscribe<N, P, SR, UR>(
  host: SubscriptionHost<N, P, SR, UR>, channel: string, params?: P, options: SubscribeOptions<N> = {},
): Promise<Stream<N, SR, UR>> {
  const subscriptionChannel = options.requestChannel || channel
  if (options.messageKey) host.messageKeys.set(subscriptionChannel, options.messageKey)
  if (host.subscriptions.has(channel)) throw new LogicError(`Already subscribed to channel "${channel}"`)

  const queue = new AsyncQueue<N>()
  host.subscriptions.set(channel, queue)
  let reply: SR
  try {
    reply = await host.wait(host.requestSubscription(subscriptionChannel, params))
  } catch (e) {
    host.subscriptions.delete(channel)
    throw e
  }

  const unsubscribed = new Deferred<UR>()

  async function* stream(): AsyncGenerator<N> {
    while (true) {
      let next: { done: true } | { done: false; item: N }
      try {
        next = await host.wait(Promise.race([
          unsubscribed.promise.then((): { done: true } => ({ done: true })),
          queue.pull().then((item): { done: false; item: N } => ({ done: false, item })),
        ]))
      } catch (e) {
        host.subscriptions.delete(channel)
        throw e
      }
      if (next.done) break
      yield next.item
    }
  }

  async function unsubscribe(): Promise<UR | undefined> {
    if (unsubscribed.settled) return unsubscribed.promise
    const reply = await host.wait(host.requestUnsubscription(subscriptionChannel, params))
    unsubscribed.resolve(reply)
    host.subscriptions.delete(channel)
    return reply
  }

  return new Stream(reply, stream(), unsubscribe)
}

/**
 * Multiplexed subscribe/stream client.
 *
 * Requires implementing `requestSubscription`, `requestUnsubscription` and `parseMsg`.
 *
 * Contract: one `subscribe()` per local channel at a time.
 */
export abstract class Streams<Notification = unknown, SubscriptionParams = unknown, SubscriptionReply = unknown, UnsubscriptionReply = unknown>
  extends Socket implements SubscriptionHost<Notification, SubscriptionParams, SubscriptionReply, UnsubscriptionReply> {
  readonly subscriptions = new Map<string, AsyncQueue<Notification>>()
  readonly messageKeys = new Map<string, (notification: Notification) => string>()

  abstract requestSubscription(channel: string, params?: SubscriptionParams): Promise<SubscriptionReply>
  abstract requestUnsubscription(channel: string, params?: SubscriptionParams): Promise<UnsubscriptionReply>
  /** Parse a frame into a channel push, or `null` for frames that are not one. */
  abstract parseMsg(msg: Data): ChannelMessage<Notification> | null

  override async close(): Promise<void> {
    await super.close()
    this.subscriptions.clear()
  }

  onMsg(msg: Data): void {
    const res = this.parseMsg(msg)
    if (res !== null) dispatch(this, res)
  }

  /**
   * Subscribe to `channel`, the local channel identifier; `options.requestChannel` names
   * the channel in the request when it differs, `options.messageKey` derives the local
   * identifier from each notification when the channel alone cannot.
   */
  subscribe(channel: string, params?: SubscriptionParams, options?: SubscribeOptions<Notification>):
    Subscription<Notification, SubscriptionReply, UnsubscriptionReply> {
    return new Subscription(() => subscribe(this, channel, params, options))
  }
}
