/** Multiplexed request/response client via message ids. */
import { Deferred } from './async.js'
import { Socket, type Context, type Data } from './socket.js'

/** A parsed reply: the id of the request it answers, and the reply itself. */
export interface Response<Reply> {
  id: number
  reply: Reply
}

/** The pieces of a socket `rpcRequest` needs; `Rpc` and `StreamsRpc` both provide them. */
export interface RpcHost<Request, Reply> extends Socket {
  readonly replies: Map<number, Deferred<Reply>>
  counter: number
  rpcSend(id: number, request: Request): Promise<void>
}

/** Send `request` under a fresh id and wait for its reply: the `Rpc.rpcRequest` body, shared with `StreamsRpc`. */
export async function rpcRequest<Req, Rep>(host: RpcHost<Req, Rep>, request: Req, ctx?: Context): Promise<Rep> {
  const id = host.counter++
  const reply = new Deferred<Rep>()
  host.replies.set(id, reply)
  try {
    await host.rpcSend(id, request)
    return await host.wait(reply.promise, ctx)
  } finally {
    host.replies.delete(id)
  }
}

/**
 * Multiplexed request/response client: ids are managed here, `rpcRequest` is agnostic to
 * them.
 *
 * Requires implementing `parseResponse` and `rpcSend`. Many concurrent `rpcRequest()`
 * calls are fine.
 */
export abstract class Rpc<Request = unknown, Reply = unknown> extends Socket implements RpcHost<Request, Reply> {
  readonly replies = new Map<number, Deferred<Reply>>()
  counter = 0

  /** Parse a frame into an identified reply, or `null` for frames that are not one. */
  abstract parseResponse(msg: Data): Response<Reply> | null
  /** Write `request` to the wire, tagged with `id` the way the protocol correlates replies. */
  abstract rpcSend(id: number, request: Request): Promise<void>

  override async close(): Promise<void> {
    await super.close()
    this.replies.clear()
  }

  onMsg(msg: Data): void {
    const response = this.parseResponse(msg)
    if (response !== null) this.replies.get(response.id)?.resolve(response.reply)
  }

  rpcRequest(request: Request): Promise<Reply> {
    return rpcRequest(this, request)
  }
}
