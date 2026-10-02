/**
 * The contract's shapes, as a generated class and a hand-written core meet them: a stream
 * declaring its `reply` (ADR 0014) gets a typed `Stream.reply`, and a dual-transport call
 * always carries its transport.
 */
import { describe, expect, expectTypeOf, it } from 'vitest'
import type { DualEndpoint, ReplyStreamEndpoint, StreamEndpoint, SubscribeCall, TransportCall } from '../src/contract.js'
import * as t from '../src/validation.js'
import type { Codec } from '../src/validation.js'
import { Stream, Subscription } from '../src/ws/streams.js'

interface Ack { channel: string; depth: number }
const Ack: Codec<Ack> = t.object({ channel: t.string, depth: t.integer })

/** A core over canned frames, validating the ack through `replyCodec` and each push through `messageCodec`. */
class CannedCore implements ReplyStreamEndpoint, StreamEndpoint {
  constructor(readonly ack: unknown, readonly pushes: unknown[]) {}

  subscribe<Params, Message, Reply = unknown>(call: SubscribeCall<Params, Message, Record<string, never>, Reply>): Subscription<Message, Reply> {
    const validate = call.validate ?? true
    const { ack, pushes } = this
    async function* messages(): AsyncGenerator<Message> {
      for (const push of pushes) yield (validate && call.messageCodec !== undefined ? call.messageCodec.parse(push) : push) as Message
    }
    // Validated on connect, as a socket core does when the ack arrives: a bad ack rejects the await.
    return new Subscription(async () => {
      const reply = (validate && call.replyCodec !== undefined ? call.replyCodec.parse(ack) : ack) as Reply
      return new Stream(reply, messages(), async () => undefined)
    })
  }
}

describe('contract', () => {
  it('a declared reply is parsed through replyCodec and typed on the stream', async () => {
    const core: ReplyStreamEndpoint = new CannedCore({ channel: 'book', depth: '10' }, [])
    const subscription = core.subscribe({
      channel: 'book', parameters: undefined, parametersCodec: undefined,
      messageCodec: t.object({ a: t.integer }), replyCodec: Ack, meta: {},
    })
    expectTypeOf(subscription).toEqualTypeOf<Subscription<{ a: number }, Ack>>()
    await expect(subscription.then(stream => stream.reply)).rejects.toThrow(/depth/)
    const ok = await new CannedCore({ channel: 'book', depth: 10 }, []).subscribe({
      channel: 'book', parameters: undefined, parametersCodec: undefined, messageCodec: undefined, replyCodec: Ack, meta: {},
    })
    expect(ok.reply).toEqual({ channel: 'book', depth: 10 })
  })

  it('a stream without a reply keeps an untyped one', () => {
    const core: StreamEndpoint = new CannedCore({}, [])
    const subscription = core.subscribe({ channel: 'x', parameters: undefined, parametersCodec: undefined, messageCodec: undefined, meta: {} })
    expectTypeOf(subscription).toEqualTypeOf<Subscription<unknown>>()
  })

  it('a dual-transport call carries its transport', () => {
    expectTypeOf<TransportCall<unknown, unknown, Record<string, never>>['transport']>().toEqualTypeOf<'http' | 'ws'>()
    expectTypeOf<Parameters<DualEndpoint['request']>[0]>().toHaveProperty('transport')
  })
})
