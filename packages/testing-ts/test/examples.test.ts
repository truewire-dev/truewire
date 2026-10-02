import { describe, expect, it } from 'vitest'
import { endpointRecords, httpExamples, wsExamples } from '../src/examples.js'
import { fixtureRoot } from './setup.js'

describe('example discovery', () => {
  it('reads every endpoint with its function path and transports', () => {
    const records = endpointRecords(fixtureRoot)
    expect(records.map(r => [r.function, r.kind, r.transports, r.dual])).toEqual([
      ['pets.adoptions', 'rpc', ['http'], false],
      ['pets.get_pet', 'rpc', ['http', 'ws'], true],
      ['pets.list_pets', 'rpc', ['ws', 'http'], true],
    ])
  })

  it('pairs HTTP request and response recordings', () => {
    const examples = httpExamples(fixtureRoot)
    expect(examples.map(e => [e.endpoint.function, e.id, e.request, e.status])).toEqual([
      ['pets.adoptions', 'page1', { from: 0, to: 9, limit: 3 }, 200],
      ['pets.adoptions', 'page2', { from: 2, to: 9, limit: 3 }, 200],
      ['pets.adoptions', 'page3', { from: 4, to: 9, limit: 3 }, 200],
      ['pets.get_pet', 'default', { petId: 42 }, 200],
    ])
  })

  it('pairs native WebSocket recordings and synthesizes the rest from HTTP for a dual endpoint', () => {
    const examples = wsExamples(fixtureRoot)
    expect(examples.map(e => [e.endpoint.function, e.id, e.synthesized])).toEqual([
      ['pets.get_pet', 'default', true],
      ['pets.list_pets', 'two', false],
    ])
    const [synthesized, native] = examples
    expect(synthesized!.payload).toEqual({ jsonrpc: '2.0', id: 0, method: 'pets_get', params: { petId: 42 } })
    expect(synthesized!.reply).toEqual({ jsonrpc: '2.0', id: 1, result: { id: 42, name: 'Fido' } })
    expect(native!.parameters).toEqual({ limit: 2 })
  })
})
