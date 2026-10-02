/**
 * gRPC for generated TypeScript clients (ADR 0017): the contract a generated gRPC endpoint
 * calls, and `GrpcClient`, the core that satisfies it over HTTP/2.
 *
 * A generated gRPC endpoint class takes a `GrpcEndpoint<Meta>` and calls one verb on it,
 * `unary`, with the method descriptor from the protobuf-es stubs `truewire protos
 * typescript` builds out of `spec/proto/`, the request (any `MessageInitShape` of the
 * input message) and the endpoint's `meta`. The core returns the decoded response message;
 * there is no `validate` switch, since a protobuf message is decoded by its schema.
 *
 * Imported from `@truewire/core/grpc` only: it needs `@bufbuild/protobuf`,
 * `@connectrpc/connect` and `@connectrpc/connect-node`, which the package declares as
 * optional peer dependencies so an HTTP/WebSocket client installs none of them.
 */
import type { DescMessage, DescMethodUnary, MessageInitShape, MessageShape } from '@bufbuild/protobuf'
import { Code, ConnectError, type Transport } from '@connectrpc/connect'
import { Http2SessionManager, createGrpcTransport } from '@connectrpc/connect-node'
import { ApiError, AuthError, BadRequest, NetworkError, RateLimited, type ApiErrorOptions } from './errors.js'

/** Request metadata: a `Headers`, a plain record or `[name, value]` pairs. */
export type GrpcHeaders = Headers | Record<string, string> | [string, string][]

/** Options every generated gRPC method takes as its last parameter. */
export interface GrpcCallOptions {
  /** Abort the call. */
  signal?: AbortSignal
  /** Abandon the call after this many milliseconds (a `NetworkError`); the client's default when omitted. */
  timeoutMs?: number
  /** Request metadata sent with this call, beside the client's own. */
  headers?: GrpcHeaders
}

/** One unary call: the stub's method descriptor, the request and the endpoint's `meta`. */
export interface GrpcCall<I extends DescMessage, O extends DescMessage, Meta> extends GrpcCallOptions {
  /** The method descriptor, `Service.method.<rpc>` of the generated `_pb.ts` stub. */
  method: DescMethodUnary<I, O>
  request: MessageInitShape<I>
  meta: Meta
}

/** Base of a generated gRPC endpoint: `unary` sends one call and returns the response message. */
export interface GrpcEndpoint<Meta = Record<string, never>> {
  unary<I extends DescMessage, O extends DescMessage>(call: GrpcCall<I, O, Meta>): Promise<MessageShape<O>>
}

/** The body a gRPC `ApiError` carries: the status the server returned. */
export interface GrpcStatus {
  /** The numeric gRPC status code (`3` for `INVALID_ARGUMENT`). */
  code: number
  /** The status code's name, `invalid_argument`. */
  name: string
  message: string
}

/**
 * A gRPC failure as the error taxonomy every runtime shares: `UNAVAILABLE` and
 * `DEADLINE_EXCEEDED` (the connection, not the API) are a `NetworkError`; an API status is
 * an `ApiError` (`BadRequest`, `AuthError`, `RateLimited` where one fits) whose `body` is
 * the `GrpcStatus`. A value that is not a gRPC error is returned unchanged.
 */
export function grpcError(error: unknown): unknown {
  if (error instanceof ApiError || error instanceof NetworkError) return error
  const connect = error instanceof ConnectError ? error : error instanceof Error && error.name === 'AbortError' ? undefined : ConnectError.from(error)
  if (connect === undefined) return error
  if (connect.code === Code.Canceled && !(error instanceof ConnectError)) return error
  const status: GrpcStatus = { code: connect.code, name: Code[connect.code]?.replace(/([a-z])([A-Z])/g, '$1_$2').toLowerCase() ?? 'unknown', message: connect.rawMessage }
  const options: ApiErrorOptions = { cause: error, body: status }
  switch (connect.code) {
    case Code.Unavailable:
    case Code.DeadlineExceeded:
      return new NetworkError(connect.message, { cause: error })
    case Code.Unauthenticated:
    case Code.PermissionDenied:
      return new AuthError(connect.message, options)
    case Code.ResourceExhausted:
      return new RateLimited(connect.message, options)
    case Code.InvalidArgument:
    case Code.FailedPrecondition:
    case Code.OutOfRange:
    case Code.NotFound:
      return new BadRequest(connect.message, options)
    default:
      return new ApiError(connect.message, options)
  }
}

export interface GrpcClientOptions {
  /** The server, `https://host:443` (or `http://` for a plaintext HTTP/2 server). */
  baseUrl: string
  /** A transport to call through instead of the HTTP/2 one this client opens itself. */
  transport?: Transport
  /** The default per-call timeout, in milliseconds. */
  timeoutMs?: number
  /** Metadata sent with every call. */
  headers?: GrpcHeaders
}

/**
 * A gRPC core over one HTTP/2 connection, opened on the first call and released by
 * `close()`. It satisfies `GrpcEndpoint` for any `Meta`, so one client serves every gRPC
 * endpoint of a generated tree; a hand-written core that reads `meta` wraps it.
 */
export class GrpcClient implements GrpcEndpoint<unknown> {
  #transport: Transport | undefined
  #sessions: Http2SessionManager | undefined

  constructor(readonly options: GrpcClientOptions) {
    this.#transport = options.transport
  }

  /** The transport calls go through, opening the HTTP/2 session manager on first use. */
  get transport(): Transport {
    if (this.#transport === undefined) {
      this.#sessions = new Http2SessionManager(this.options.baseUrl)
      this.#transport = createGrpcTransport({ baseUrl: this.options.baseUrl, sessionManager: this.#sessions })
    }
    return this.#transport
  }

  async unary<I extends DescMessage, O extends DescMessage>(call: GrpcCall<I, O, unknown>): Promise<MessageShape<O>> {
    const headers = new Headers(this.options.headers)
    new Headers(call.headers).forEach((value, key) => headers.set(key, value))
    try {
      const response = await this.transport.unary(
        call.method, call.signal, call.timeoutMs ?? this.options.timeoutMs, headers, call.request,
      )
      return response.message
    } catch (error) {
      throw grpcError(error)
    }
  }

  /** Close the HTTP/2 connection this client opened; the next call opens a new one. */
  close(): void {
    this.#sessions?.abort()
    if (this.#sessions !== undefined) this.#transport = this.options.transport
    this.#sessions = undefined
  }

  [Symbol.dispose](): void {
    this.close()
  }
}
