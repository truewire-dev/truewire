/**
 * Hand-written core for the `pets` fixture: JSON-RPC 2.0 over HTTP `POST` and over a
 * WebSocket, one `DualEndpoint` routing each call by its `transport`.
 */
import { HttpClient, LogicError, ws, type DualEndpoint, type TransportCall } from '@truewire/core'

interface Frame {
  jsonrpc: '2.0'
  method: string
  params: unknown
}

interface Reply {
  jsonrpc: string
  id: number
  result?: unknown
  error?: { code: number; message: string }
}

/** The WebSocket half: replies correlated by the frame's `id`. */
export class RpcSocket extends ws.Rpc<Frame, Reply> {
  parseResponse(msg: ws.Data): ws.Response<Reply> | null {
    const frame = JSON.parse(typeof msg === 'string' ? msg : new TextDecoder().decode(msg)) as Reply
    return typeof frame.id === 'number' ? { id: frame.id, reply: frame } : null
  }

  async rpcSend(id: number, request: Frame): Promise<void> {
    ;(await this.ws).send(JSON.stringify({ ...request, id }))
  }
}

export interface CoreOptions {
  baseUrl: string
  wsUrl: string
}

export class Core implements DualEndpoint {
  readonly http = new HttpClient()
  readonly socket: RpcSocket
  readonly baseUrl: string
  private nextId = 1

  constructor(options: CoreOptions) {
    this.baseUrl = options.baseUrl
    this.socket = new RpcSocket({ url: options.wsUrl })
  }

  async request<Req, Res>(call: TransportCall<Req, Res, Record<string, never>>): Promise<Res> {
    const params = call.request !== undefined && call.requestCodec !== undefined ? call.requestCodec.dump(call.request) : {}
    const frame: Frame = { jsonrpc: '2.0', method: call.path, params }
    let reply: Reply
    if (call.transport === 'ws') {
      reply = await this.socket.rpcRequest(frame)
    } else {
      const response = await this.http.request(call.method ?? 'POST', this.baseUrl, { json: { ...frame, id: this.nextId++ }, signal: call.signal })
      reply = (await response.json()) as Reply
    }
    if (reply.error !== undefined) throw new LogicError(`${call.path}: ${reply.error.message}`)
    if (call.responseCodec === undefined) return undefined as Res
    return (call.validate ?? true) ? call.responseCodec.parse(reply.result) : (reply.result as Res)
  }

  close(): Promise<void> {
    return this.socket.close()
  }
}
