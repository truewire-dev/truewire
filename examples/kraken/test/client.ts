/**
 * A `Kraken` client whose three transports all point at the local mock: the REST one
 * with a fake key pair (the mock checks the request's shape, never `API-Sign`), and the
 * two sockets on one URL with no token source, since the recorded subscribe frames
 * carry none. Built from the transports directly rather than through `Core`, the way
 * `conftest.py` builds the Python client from its raw transports.
 */
import { inject } from 'vitest'
import { SocketCore, SpotCore, type Credentials } from '../src/kraken/core/index.js'
import { Kraken } from '../src/kraken/index.js'

/** Never real: the private key is base64 of thirty-two zero bytes. */
export const FAKE_CREDENTIALS: Credentials = {
  apiKey: 'mock-api-key',
  privateKey: 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=',
}

export interface Transports {
  spot_client: SpotCore
  market_client: SocketCore
  private_client: SocketCore
}

export function mockTransports(): Transports {
  return {
    spot_client: new SpotCore({ baseUrl: inject('httpBaseUrl'), credentials: FAKE_CREDENTIALS }),
    market_client: new SocketCore({ url: inject('wsUrl') }),
    private_client: new SocketCore({ url: inject('wsUrl') }),
  }
}

export function mockClient(): Kraken {
  return new Kraken(mockTransports())
}

/** Run `body` with a client on fresh transports, closing both sockets afterwards. */
export async function withClient<T>(body: (client: Kraken, transports: Transports) => Promise<T>): Promise<T> {
  const transports = mockTransports()
  try {
    return await body(new Kraken(transports), transports)
  } finally {
    await Promise.all([transports.market_client.close(), transports.private_client.close()])
  }
}
