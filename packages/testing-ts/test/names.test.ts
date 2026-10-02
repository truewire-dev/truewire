import { describe, expect, it } from 'vitest'
import { camelCase, endpointModulePath, resolveMethod } from '../src/names.js'

describe('names', () => {
  it('applies the generator\'s camelCase rule', () => {
    expect(camelCase('list_commits')).toBe('listCommits')
    expect(camelCase('get')).toBe('get')
    expect(camelCase('getRepo')).toBe('getRepo')
    expect(camelCase('market-data')).toBe('marketData')
    expect(camelCase('Trading_ws')).toBe('tradingWs')
  })

  it('resolves a function path to a bound method, or undefined', async () => {
    const client = { tradingWs: { prefix: 'ok', addOrder(this: { prefix: string }) { return this.prefix } } }
    expect(resolveMethod(client, 'trading_ws.add_order')?.()).toBe('ok')
    expect(resolveMethod(client, 'trading_ws.missing')).toBeUndefined()
    expect(resolveMethod(client, 'nope.add_order')).toBeUndefined()
  })

  it('maps a function path to its endpoint module', () => {
    expect(endpointModulePath('/p/src/kraken/', 'trading_ws.add_order')).toBe('/p/src/kraken/trading_ws/add_order.ts')
  })
})
