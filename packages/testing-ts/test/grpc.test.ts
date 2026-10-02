/**
 * gRPC (ADR 0017) end to end: the `grpc_client` fixture's generated package (committed as
 * `grpc_demo/`, pinned to the backend by `test_codegen_typescript_grpc.py`) over
 * `GrpcClient`, against `startGrpcMock` serving the fixture's recordings.
 */
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { BadRequest } from '@truewire/core'
import { GrpcClient } from '@truewire/core/grpc'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { describeGrpcReplay, grpcExamples, startGrpcMock, type GrpcMock } from '../src/grpc.js'
import { GrpcDemo } from './grpc_demo/index.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(here, '../../truewire/test/fixtures/grpc_client')
const packageDir = path.join(here, 'grpc_demo')
const importModule = (url: string) => import(/* @vite-ignore */ url)
let mock: GrpcMock

beforeAll(async () => {
  mock = await startGrpcMock({ projectRoot, packageDir, importModule })
})
afterAll(async () => {
  await mock.close()
})

async function withClient(body: (client: GrpcDemo) => Promise<void>): Promise<void> {
  const core = new GrpcClient({ baseUrl: mock.baseUrl })
  try {
    await body(new GrpcDemo(core))
  } finally {
    core.close()
  }
}

describeGrpcReplay({ test: { describe, it }, projectRoot, packageDir, withClient, importModule })

describe('gRPC examples and generated walkers', () => {
  it('discovers every recording of every gRPC endpoint', () => {
    expect(grpcExamples(projectRoot).map(example => `${example.endpoint.function}/${example.id}`)).toEqual([
      'bank.all_balances/01', 'bank.all_balances/02', 'bank.balance/01', 'bank.search/01',
    ])
  })

  it('walks a token pagination without touching the caller\'s request', async () => {
    await withClient(async client => {
      const request = { address: 'demo1qqqsyqcyq5rqwzqfpg9scrgwpugpzysn', pagination: { limit: 2n } }
      const before = mock.answered('bank.all_balances')
      const rows = await client.bank.allBalancesPaged(request)
      expect(rows.map(row => row.denom)).toEqual(['uatom', 'udemo', 'uusdc'])
      expect(mock.answered('bank.all_balances') - before).toBe(2)
      expect(request.pagination).toEqual({ limit: 2n })
    })
  })

  it('walks a page pagination ended by its total', async () => {
    await withClient(async client => {
      const rows = await client.bank.searchPaged({ query: "denom='udemo'", orderBy: 2, limit: 2n })
      expect(rows.map(row => row.score)).toEqual([9])
    })
  })

  it('answers a call no recording matches with NOT_FOUND', async () => {
    await withClient(async client => {
      await expect(client.bank.balance({ address: 'nobody' })).rejects.toBeInstanceOf(BadRequest)
    })
  })
})

describe('recordedMessage', () => {
  it('decodes an Any recorded in its field form without a registry, singular and repeated', async () => {
    const { OptionSchema, TypeSchema, StringValueSchema } = await import('@bufbuild/protobuf/wkt')
    const { create, toBinary } = await import('@bufbuild/protobuf')
    const { recordedMessage } = await import('../src/grpc.js')
    const packed = toBinary(StringValueSchema, create(StringValueSchema, { value: 'hello' }))
    const legacy = { type_url: '/google.protobuf.StringValue', value: Buffer.from(packed).toString('base64') }
    const option = recordedMessage(OptionSchema, { name: 'greeting', value: legacy })
    expect(option.name).toBe('greeting')
    expect(option.value?.typeUrl).toBe('/google.protobuf.StringValue')
    expect(option.value?.value).toEqual(packed)
    const type = recordedMessage(TypeSchema, { name: 'T', options: [{ name: 'a', value: legacy }, { name: 'b' }] })
    expect(type.options.map(o => o.name)).toEqual(['a', 'b'])
    expect(type.options[0]!.value?.value).toEqual(packed)
    expect(type.options[1]!.value).toBeUndefined()
  })

  it('still reads proto JSON, and needs a registry for an @type Any', async () => {
    const { OptionSchema, StringValueSchema } = await import('@bufbuild/protobuf/wkt')
    const { createRegistry } = await import('@bufbuild/protobuf')
    const { recordedMessage } = await import('../src/grpc.js')
    const json = { name: 'x', value: { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: 'hi' } }
    expect(() => recordedMessage(OptionSchema, json)).toThrow()
    expect(recordedMessage(OptionSchema, json, createRegistry(StringValueSchema)).value?.typeUrl).toBe('type.googleapis.com/google.protobuf.StringValue')
  })
})
