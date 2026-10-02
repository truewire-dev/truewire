/**
 * `spot.account.retrieve_export` -- hand-written, not generated.
 *
 * Its 2xx response is the raw export zip (`Content-Type: application/zip`), not the
 * `{error, result}` JSON envelope, so no response schema describes it: the endpoint's spec
 * declares `surface: handwritten`, `truewire generate typescript` renders no method for it,
 * and `truewire.toml`'s `[typescript.extras."spot.account"]` folds this class into the
 * generated `Account` router, which exposes `retrieveExport`. Signing and the path are the
 * same as any private endpoint; only the bytes return is hand-written, through
 * `SpotCore.signedBytes`, the TypeScript half of the Python module's `authed_raw_request`.
 */
import { LogicError, type CallOptions, type HttpEndpoint } from '@truewire/core'
import type { SpotMeta } from '../../meta.js'

const PATH = '/0/private/RetrieveExport'

/** What this endpoint needs beyond `HttpEndpoint<SpotMeta>`: a signed POST whose body comes back as bytes (`SpotCore` has it). */
export interface RawSpotEndpoint {
  signedBytes(path: string, values: Record<string, unknown>, signal?: AbortSignal): Promise<Uint8Array>
}

export interface Request {
  /** Report ID to retrieve. */
  id: string
}

/** `spot.account.retrieve_export`. */
export class RetrieveExport {
  constructor(readonly core: HttpEndpoint<SpotMeta>) {}

  /**
   * Retrieve a processed data export. Unlike every other Account Data endpoint, the response
   * is not the standard `{error, result}` JSON envelope: it is the raw export file itself.
   *
   * **API Key Permissions Required:** `Data - Export data`. `validate` is accepted for
   * uniformity with every other method and has no effect: there is no schema for a binary body.
   *
   * @see https://docs.kraken.com/api-reference/account-data/retrieve-data-export
   */
  async retrieveExport(request: Request, options?: CallOptions): Promise<Uint8Array> {
    const core = this.core as HttpEndpoint<SpotMeta> & Partial<RawSpotEndpoint>
    if (typeof core.signedBytes !== 'function') {
      throw new LogicError('`retrieveExport` needs a core that returns a signed raw body (`signedBytes`), as `SpotCore` does.')
    }
    return core.signedBytes(PATH, { id: request.id }, options?.signal)
  }
}
