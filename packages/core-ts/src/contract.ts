/**
 * The contract between a hand-written core and the code `truewire generate typescript`
 * emits: the TypeScript half of `truewire_core.contract`.
 *
 * A generated endpoint class takes its core as a constructor argument and calls exactly
 * one verb on it; a generated router hands the same core to every child. The generator
 * reads nothing else from a core and never imports the project's own `core/` module: the
 * interfaces below are structural, so a core satisfies them by shape and `tsc` checks the
 * two against each other where the client is constructed.
 *
 * `Meta` is the per-endpoint `meta` shape the core's `[cores.<name>]` entry declares in
 * `truewire.toml`, rendered by the generator into `<package>/meta.ts`; a core with no
 * schema receives `{}`.
 *
 * Verbs:
 *
 * - `request({ method, path, ... })`: one HTTP call (`HttpEndpoint`).
 * - `request({ path, ... })`: one WebSocket command/reply call, `path` being the wire
 *   method name (`CommandEndpoint`).
 * - `request({ method, path, transport, ... })`: one call of an endpoint declaring both
 *   transports, routed by `transport` (`DualEndpoint`).
 * - `subscribe({ channel, parameters, ... })`: one channel subscription, returning a
 *   `Subscription` the caller awaits or iterates (`StreamEndpoint`; `ReplyStreamEndpoint`
 *   when the stream declares its acknowledgement's `reply` and passes `replyCodec`).
 */
import type { Codec } from './validation.js'
import type { Subscription } from './ws/streams.js'

/** Options every generated method takes as its last parameter. */
export interface CallOptions {
  /** Override this call's response validation; the client-level default when omitted. */
  validate?: boolean
  /** Abort the call. */
  signal?: AbortSignal
}

/** What every verb carries: the generated request value, its codec, the reply's codec and the endpoint's `meta`. */
export interface Call<Req, Res, Meta> extends CallOptions {
  /**
   * The generated request value: an object whose keys are the wire's own parameter
   * names (a `Request` interface), a union member, an array, or `undefined` when the
   * endpoint declares no request.
   */
  request: Req | undefined
  /** Codec of `request`, for `dump` to the wire; `undefined` with no request. */
  requestCodec: Codec<Req> | undefined
  /** Codec of the value the method returns, for `parse`; `undefined` when it returns nothing. */
  responseCodec: Codec<Res> | undefined
  /** The endpoint's declared `meta`, in the shape its core's schema states. */
  meta: Meta
}

/** One HTTP call. `{name}` placeholders in `path` are filled from `request`. */
export interface HttpCall<Req, Res, Meta> extends Call<Req, Res, Meta> {
  /** The wire HTTP method; `undefined` when the spec leaves it to the core (a uniformly POST JSON-RPC API). */
  method: string | undefined
  /** The wire path template. */
  path: string
}

/** A transport an `rpc` endpoint can be reached over. */
export type Transport = 'http' | 'ws'

/**
 * Options of a method whose endpoint declares both `http` and `ws` transports: the call
 * options plus which transport this call goes over. Omitted, the endpoint's first-declared
 * transport is used.
 */
export interface TransportOptions extends CallOptions {
  transport?: Transport
}

/**
 * One call of an endpoint that declares both transports. It carries everything an
 * `HttpCall` does (the HTTP method may be `undefined` for a uniformly POST JSON-RPC API);
 * `path` is the HTTP path template or, over `ws`, the wire method name (ADR 0006: one
 * identifier regardless of transport), and `transport` is always set.
 */
export interface TransportCall<Req, Res, Meta> extends HttpCall<Req, Res, Meta> {
  transport: Transport
}

/** One WebSocket command; `path` is the wire method name. */
export interface CommandCall<Req, Res, Meta> extends Call<Req, Res, Meta> {
  path: string
}

/** One channel subscription; `{name}` placeholders in `channel` are filled from `parameters`. */
export interface SubscribeCall<Params, Message, Meta, Reply = unknown> extends CallOptions {
  channel: string
  parameters: Params | undefined
  parametersCodec: Codec<Params> | undefined
  /** Codec of each pushed message. */
  messageCodec: Codec<Message> | undefined
  /**
   * Codec of the subscription's acknowledgement, the `Stream.reply` value (ADR 0014); set
   * only by a stream that declares `reply`, whose class takes a `ReplyStreamEndpoint`. A
   * core validates the ack through it and never through `messageCodec`, and vice versa.
   */
  replyCodec?: Codec<Reply> | undefined
  meta: Meta
}

/**
 * Base of a generated `rpc` endpoint reached over HTTP.
 *
 * `request` sends one call and returns the value the method's return type describes:
 * `responseCodec.parse` of the reply's JSON when validation is on, the raw `JSON.parse`
 * value (cast) when `validate` is `false`.
 */
export interface HttpEndpoint<Meta = Record<string, never>> {
  request<Req, Res>(call: HttpCall<Req, Res, Meta>): Promise<Res>
}

/** Base of a generated `rpc` endpoint reached over a WebSocket connection. */
export interface CommandEndpoint<Meta = Record<string, never>> {
  request<Req, Res>(call: CommandCall<Req, Res, Meta>): Promise<Res>
}

/**
 * Base of a generated `rpc` endpoint that declares both `http` and `ws` transports.
 *
 * The generated method passes `transport` (the caller's choice, else the first-declared
 * one) and the core routes the call to its HTTP client or its socket: the TypeScript half
 * of Python's `request(..., transport=...)`.
 */
export interface DualEndpoint<Meta = Record<string, never>> {
  request<Req, Res>(call: TransportCall<Req, Res, Meta>): Promise<Res>
}

/** Base of a generated `stream` endpoint. */
export interface StreamEndpoint<Meta = Record<string, never>> {
  subscribe<Params, Message>(call: SubscribeCall<Params, Message, Meta>): Subscription<Message>
}

/**
 * Base of a generated `stream` endpoint that declares its subscription `reply` (ADR 0014):
 * the core parses the acknowledgement through `replyCodec` (unless `validate` is off) and
 * hands it back as the stream's typed `reply`. Adopting a declared `reply` is a per-core
 * step: a core that only satisfies `StreamEndpoint` does not type-check here.
 */
export interface ReplyStreamEndpoint<Meta = Record<string, never>> {
  subscribe<Params, Message, Reply>(
    call: SubscribeCall<Params, Message, Meta, Reply> & { replyCodec: Codec<Reply> | undefined },
  ): Subscription<Message, Reply>
}
