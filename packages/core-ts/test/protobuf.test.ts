import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { startMock, type MockServer } from '../../testing-ts/src/mock.js'
import { LogicError, ValidationError } from '../src/errors.js'
import { Frame, isBinary, jsonName, ProtoFrames } from '../src/protobuf.js'
import { Deferred } from '../src/ws/async.js'
import type { Data } from '../src/ws/socket.js'
import { Streams, type ChannelMessage } from '../src/ws/streams.js'

/** A protobuf-framed project: mexc's spot trade stream, its `.proto` and the decoded-frame goldens. */
const fixture = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../testing-ts/test/fixture-protobuf')

interface Golden { endpoint: string; proto_field: string; data: string; json: Record<string, unknown> }
const golden = JSON.parse(readFileSync(path.join(fixture, 'frames.golden.json'), 'utf8')) as { message: string; frames: Golden[] }

function sources(): Record<string, string> {
  const dir = path.join(fixture, 'spec', 'proto')
  return Object.fromEntries(readdirSync(dir).filter(name => name.endsWith('.proto')).map(name => [name, readFileSync(path.join(dir, name), 'utf8')]))
}

const bytes = (base64: string) => new Uint8Array(Buffer.from(base64, 'base64'))

describe('ProtoFrames', () => {
  const frames = ProtoFrames.compile(sources(), golden.message)

  it('names fields as protoc does', () => {
    expect(jsonName('public_aggre_deals')).toBe('publicAggreDeals')
    expect(jsonName('send_time')).toBe('sendTime')
    expect(jsonName('channel')).toBe('channel')
  })

  it.each(golden.frames.map(g => [g.endpoint, g] as const))('decodes the recorded %s frame to its ProtoJSON golden', (_name, g) => {
    const frame = frames.decode(bytes(g.data))
    expect(frame.json()).toEqual(g.json)
    expect(frame.has(g.proto_field)).toBe(true)
    expect(frame.oneofCase('body')).toBe(g.proto_field)
    expect(frame.field(g.proto_field)).toEqual(g.json[jsonName(g.proto_field)])
    expect(frame.string('channel')).toBe(g.json.channel)
  })

  it('decodes an ArrayBuffer as a socket hands it over', () => {
    const g = golden.frames[0]!
    const view = bytes(g.data)
    const buffer = view.buffer.slice(view.byteOffset, view.byteOffset + view.byteLength)
    expect(frames.decode(buffer).json()).toEqual(g.json)
  })

  it('leaves unset fields out and reports them unset', () => {
    const frame = frames.decode(bytes(golden.frames.find(g => g.proto_field === 'public_aggre_deals')!.data))
    expect(frame.has('private_orders')).toBe(false)
    expect(frame.field('private_orders')).toBeUndefined()
    expect(frame.string('symbol_id')).toBeUndefined()
  })

  it('rejects a text frame and bytes that are not the message', () => {
    expect(isBinary('{}')).toBe(false)
    expect(() => frames.decode('{"code":0}')).toThrow(ValidationError)
    expect(() => frames.decode(new Uint8Array([0x0a, 0xff]))).toThrow(ValidationError)
  })

  it('reports sources that do not compile or lack the message, and unknown fields', () => {
    expect(() => ProtoFrames.compile({ 'bad.proto': 'message {' }, 'X')).toThrow(LogicError)
    expect(() => ProtoFrames.compile(sources(), 'NoSuchMessage')).toThrow(LogicError)
    expect(() => frames.decode(bytes(golden.frames[0]!.data)).has('no_such_field')).toThrow(LogicError)
  })

  it('renders 64-bit integers as strings, bytes as base64 and enums by name', () => {
    const extra = ProtoFrames.compile({
      'extra.proto': 'syntax = "proto3"; enum Side { SIDE_UNSET = 0; BUY = 1; } message Extra { int64 big = 1; bytes raw = 2; Side side = 3; repeated uint64 ids = 4; optional int32 zero = 5; }',
    }, 'Extra')
    // big=9007199254740993, raw=0x01 0x02, side=BUY, ids=[1,2], zero=0 (explicitly present)
    const frame = extra.decode(new Uint8Array([0x08, 0x81, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x10, 0x12, 0x02, 0x01, 0x02, 0x18, 0x01, 0x22, 0x02, 0x01, 0x02, 0x28, 0x00]))
    expect(frame.json()).toEqual({ big: '9007199254740993', raw: 'AQI=', side: 'BUY', ids: ['1', '2'], zero: 0 })
  })
})

/**
 * A minimal protobuf-framed streams core, the shape mexc's spot core takes: JSON
 * subscribe/ack, binary pushes decoded into `Frame`s keyed by their `channel` field.
 */
class ProtobufStreams extends Streams<Frame> {
  #ack: Deferred<unknown> | null = null

  constructor(url: string, readonly frames: ProtoFrames) {
    super({ url })
  }

  async requestSubscription(channel: string): Promise<unknown> {
    const ack = (this.#ack = new Deferred<unknown>())
    ;(await this.ws).send(JSON.stringify({ method: 'SUBSCRIPTION', params: [channel] }))
    return ack.promise
  }

  async requestUnsubscription(channel: string): Promise<unknown> {
    ;(await this.ws).send(JSON.stringify({ method: 'UNSUBSCRIPTION', params: [channel] }))
    return undefined
  }

  parseMsg(msg: Data): ChannelMessage<Frame> | null {
    if (!isBinary(msg)) {
      this.#ack?.resolve(JSON.parse(msg))
      return null
    }
    const frame = this.frames.decode(msg)
    return { channel: frame.string('channel')!, notification: frame }
  }
}

describe('protobuf frames replayed by truewire mock', () => {
  let mock: MockServer

  beforeAll(async () => { mock = await startMock({ project: fixture, timeout: 60_000 }) }, 90_000)
  afterAll(async () => { await mock?.close() })

  it('subscribes over JSON and reads the recorded binary push, narrowed to meta.proto_field', async () => {
    const example = JSON.parse(readFileSync(path.join(fixture, 'spec/endpoints/market/trades/examples/doc.parameters.json'), 'utf8')) as { payload: { params: string[] } }
    const endpoint = JSON.parse(readFileSync(path.join(fixture, 'spec/endpoints/market/trades/endpoint.json'), 'utf8')) as { meta: { proto_field: string } }
    const expected = golden.frames.find(g => g.endpoint === 'spot.streams.market.trades')!
    const field = endpoint.meta.proto_field

    await using client = new ProtobufStreams(mock.wsUrl!, ProtoFrames.compile(sources(), golden.message))
    const stream = await client.subscribe(example.payload.params[0]!).filter(frame => frame.has(field)).map(frame => frame.field(field))
    expect(stream.reply).toMatchObject({ code: 0 })
    const first = await stream[Symbol.asyncIterator]().next()
    expect(first.done).toBe(false)
    expect(first.value).toEqual(expected.json[jsonName(field)])
  })
})
