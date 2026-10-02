import http2 from 'node:http2'
import type { AddressInfo } from 'node:net'
import { create } from '@bufbuild/protobuf'
import { Code, ConnectError } from '@connectrpc/connect'
import { connectNodeAdapter } from '@connectrpc/connect-node'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { ApiError, AuthError, BadRequest, NetworkError, RateLimited } from '../src/errors.js'
import { GrpcClient, grpcError, type GrpcEndpoint, type GrpcStatus } from '../src/grpc.js'
import { Echo, SayRequestSchema } from './fixtures/echo/v1/echo_pb.js'

let server: http2.Http2Server
let baseUrl: string
const seen: { authority?: string; trace?: string | null }[] = []

beforeAll(async () => {
  const handler = connectNodeAdapter({
    grpc: true,
    grpcWeb: false,
    connect: false,
    routes: router => {
      router.service(Echo, {
        say(request, context) {
          seen.push({ trace: context.requestHeader.get('x-trace') })
          if (request.text === 'invalid') throw new ConnectError('no such text', Code.InvalidArgument)
          if (request.text === 'auth') throw new ConnectError('who are you', Code.Unauthenticated)
          if (request.text === 'slow') throw new ConnectError('slow down', Code.ResourceExhausted)
          if (request.text === 'down') throw new ConnectError('maintenance', Code.Unavailable)
          if (request.text === 'boom') throw new ConnectError('internal', Code.Internal)
          return {
            text: request.text.repeat(Number(request.times)),
            authority: context.requestHeader.get('x-client') ?? '',
            tag: request.tag,
          }
        },
      })
    },
  })
  server = http2.createServer(handler)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
})

afterAll(async () => {
  await new Promise<void>(resolve => server.close(() => resolve()))
})

describe('GrpcClient', () => {
  it('sends a unary call and returns the decoded message', async () => {
    const client = new GrpcClient({ baseUrl, headers: { 'x-client': 'truewire' } })
    try {
      const response = await client.unary({
        method: Echo.method.say,
        request: { text: 'ab', times: 2n, tag: new Uint8Array([1, 2]) },
        meta: {},
        headers: { 'x-trace': 't-1' },
      })
      expect(response.text).toBe('abab')
      expect(response.authority).toBe('truewire')
      expect([...response.tag]).toEqual([1, 2])
      expect(seen.at(-1)?.trace).toBe('t-1')
      // A full message is a valid request too.
      const again = await client.unary({ method: Echo.method.say, request: create(SayRequestSchema, { text: 'x', times: 3n }), meta: {} })
      expect(again.text).toBe('xxx')
    } finally {
      client.close()
    }
  })

  it('satisfies the generated contract for any meta', async () => {
    const client = new GrpcClient({ baseUrl })
    const core: GrpcEndpoint<{ public: boolean }> = client
    try {
      const response = await core.unary({ method: Echo.method.say, request: { text: 'y', times: 1n }, meta: { public: true } })
      expect(response.text).toBe('y')
    } finally {
      client.close()
    }
  })

  it.each([
    ['invalid', BadRequest, 'invalid_argument', 3],
    ['auth', AuthError, 'unauthenticated', 16],
    ['slow', RateLimited, 'resource_exhausted', 8],
    ['boom', ApiError, 'internal', 13],
  ] as const)('maps the %s status into the error taxonomy', async (text, type, name, code) => {
    const client = new GrpcClient({ baseUrl })
    try {
      const error = await client.unary({ method: Echo.method.say, request: { text, times: 1n }, meta: {} }).catch((e: unknown) => e)
      expect(error).toBeInstanceOf(type)
      expect((error as ApiError).body).toEqual({ code, name, message: expect.any(String) } satisfies GrpcStatus)
      expect((error as ApiError).cause).toBeInstanceOf(ConnectError)
    } finally {
      client.close()
    }
  })

  it('reports UNAVAILABLE and an unreachable server as a NetworkError', async () => {
    const client = new GrpcClient({ baseUrl })
    try {
      await expect(client.unary({ method: Echo.method.say, request: { text: 'down', times: 1n }, meta: {} })).rejects.toBeInstanceOf(NetworkError)
    } finally {
      client.close()
    }
    const closed = http2.createServer()
    await new Promise<void>(resolve => closed.listen(0, '127.0.0.1', resolve))
    const port = (closed.address() as AddressInfo).port
    await new Promise<void>(resolve => closed.close(() => resolve()))
    const unreachable = new GrpcClient({ baseUrl: `http://127.0.0.1:${port}`, timeoutMs: 2000 })
    try {
      await expect(unreachable.unary({ method: Echo.method.say, request: { text: 'a', times: 1n }, meta: {} })).rejects.toBeInstanceOf(NetworkError)
    } finally {
      unreachable.close()
    }
  })

  it('reopens a connection after close', async () => {
    const client = new GrpcClient({ baseUrl })
    await client.unary({ method: Echo.method.say, request: { text: 'a', times: 1n }, meta: {} })
    client.close()
    const response = await client.unary({ method: Echo.method.say, request: { text: 'b', times: 1n }, meta: {} })
    expect(response.text).toBe('b')
    client.close()
  })

  it('leaves errors that are not gRPC errors alone', () => {
    const network = new NetworkError('x')
    expect(grpcError(network)).toBe(network)
    const abort = new DOMException('aborted', 'AbortError')
    expect(grpcError(abort)).toBe(abort)
  })
})
