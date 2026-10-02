import { describe, expect, inject, it } from 'vitest'
import { parseReadyLine, startMock } from '../src/mock.js'

describe('truewire mock', () => {
  it('parses the lines the mock prints on start-up', () => {
    expect(parseReadyLine('HTTP  http://127.0.0.1:5000/')).toEqual({ http: 'http://127.0.0.1:5000' })
    expect(parseReadyLine('WS    ws://127.0.0.1:5001/ws')).toEqual({ ws: 'ws://127.0.0.1:5001/ws' })
    expect(parseReadyLine('WS    (no websocket examples in this project)')).toEqual({ ws: null })
    expect(parseReadyLine('Serving recorded examples; Ctrl-C to stop.')).toBeNull()
  })

  it('parses the JSON ready line `truewire mock --json` prints', () => {
    expect(parseReadyLine('{"event": "ready", "http": "http://127.0.0.1:5000/", "ws": "ws://127.0.0.1:5001/ws"}'))
      .toEqual({ http: 'http://127.0.0.1:5000', ws: 'ws://127.0.0.1:5001/ws' })
    expect(parseReadyLine('{"event": "ready", "http": "http://127.0.0.1:5000", "ws": null}'))
      .toEqual({ http: 'http://127.0.0.1:5000', ws: null })
    expect(parseReadyLine('{"event": "stopped"}')).toBeNull()
    expect(parseReadyLine('{not json')).toBeNull()
  })

  it('the global setup provided both URLs', () => {
    expect(inject('httpBaseUrl')).toMatch(/^http:\/\//)
    expect(inject('wsUrl')).toMatch(/^ws:\/\//)
  })

  it('reports a mock that cannot start', async () => {
    await expect(startMock({ project: '/nonexistent-truewire-project', timeout: 20_000 })).rejects.toThrow(/truewire mock exited|could not start/)
  })
})
