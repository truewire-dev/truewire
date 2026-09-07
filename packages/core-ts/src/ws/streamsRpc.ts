/** Multiplexed request/response and streams client, via message ids and channel ids. */
import { LogicError } from '../errors.js'
import { AsyncQueue, Deferred } from './async.js'
import { rpcRequest, type RpcHost } from './rpc.js'
import { Socket, type Data } from './socket.js'
import {
  dispatch, subscribe, Subscription, type ChannelMessage, type SubscribeOptions, type SubscriptionHost,
} from './streams.js'

export interface RpcMessage<Reply> {
  kind: 'response'
  id: number
  response: Reply
}

export interface SubscriptionMessage<Notification> extends ChannelMessage<Notification> {
  kind: 'subscription'
}

/** What `parseMsg` yields: an identified reply, or a channel push. */
export type Message<Reply, Notification> = RpcMessage<Reply> | SubscriptionMessage<Notification>

/**
 * Multiplexed request/response and streams client.
 *
 * Requires implementing `rpcSend`, `requestSubscription`, `requestUnsubscription` and
 * `parseMsg`.
 *
 * Contract: many concurrent `rpcRequest()` calls are fine; one `subscribe()` per local
 * channel at a time.
 */
export abstract class StreamsRpc<
  Request = unknown, Reply = unknown, Notification = unknown,
  SubscriptionParams = unknown, SubscriptionReply = unknown, UnsubscriptionReply = unknown,
> extends Socket
  implements RpcHost<Request, Reply>, SubscriptionHost<Notification, SubscriptionParams, SubscriptionReply, UnsubscriptionReply> {
  readonly replies = new Map<number, Deferred<Reply>>()
  counter = 0
  readonly subscriptions = new Map<string, AsyncQueue<Notification>>()
  readonly messageKeys = new Map<string, (notification: Notification) => string>()

  abstract rpcSend(id: number, request: Request): Promise<void>
  abstract requestSubscription(channel: string, params?: SubscriptionParams): Promise<SubscriptionReply>
  abstract requestUnsubscription(channel: string, params?: SubscriptionParams): Promise<UnsubscriptionReply>
  /** Parse a frame into a reply or a channel push, or `null` for frames that are neither. */
  abstract parseMsg(msg: Data): Message<Reply, Notification> | null

  override async close(): Promise<void> {
    await super.close()
    this.subscriptions.clear()
    this.replies.clear()
  }

  rpcRequest(request: Request): Promise<Reply> {
    return rpcRequest(this, request)
  }

  onMsg(msg: Data): void {
    const res = this.parseMsg(msg)
    if (res === null) return
    if (res.kind === 'response') this.replies.get(res.id)?.resolve(res.response)
    else if (res.kind === 'subscription') dispatch(this, res)
    else throw new LogicError(`Invalid message: ${JSON.stringify(res)}`)
  }

  /** See `Streams.subscribe`. */
  subscribe(channel: string, params?: SubscriptionParams, options?: SubscribeOptions<Notification>):
    Subscription<Notification, SubscriptionReply, UnsubscriptionReply> {
    return new Subscription(() => subscribe(this, channel, params, options))
  }
}
