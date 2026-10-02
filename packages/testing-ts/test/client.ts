import { inject } from 'vitest'
import { Core } from './fixture/src/pets/core/index.js'
import { PetStore } from './fixture/src/pets/index.js'

/** Run `body` with a `PetStore` client on the mock, closing its socket afterwards. */
export async function withClient(body: (client: PetStore, core: Core) => Promise<void>): Promise<void> {
  const core = new Core({ baseUrl: inject('httpBaseUrl'), wsUrl: inject('wsUrl') })
  try {
    await body(new PetStore(core), core)
  } finally {
    await core.close()
  }
}
