/**
 * Generic replay coverage through `@truewire/testing`: every recorded HTTP example, every
 * WebSocket command and every channel subscription with a recorded push, through the real
 * client against the mock.
 *
 * Each recording is parsed through its endpoint module's `Request` (a stream's
 * `Parameters`) codec first, since a recording holds wire values and the method takes
 * typed ones; one the typed value cannot render back exactly (a sub-millisecond timestamp,
 * which a `Date` keeps to the millisecond) is skipped. Private endpoints are signed with
 * the fake key pair; the mock ignores the redacted `nonce` and matches the rest of the
 * form body. An endpoint whose `surface` is hand-written (`retrieve_export`, a binary body
 * recorded as metadata) is skipped, as it is left out of the generated package.
 *
 * A channel subscription with no recorded subscribe reply (`streams.private.executions`)
 * is left out: Kraken's socket waits for a `req_id`-correlated ack, and the mock has no
 * recorded one to replay (it only synthesizes acks for channel-envelope dialects). Every
 * other stream declares `envelope.correlate: req_id`, so its recorded ack answers the
 * client's own `req_id`.
 */
import path from 'node:path'
import { describe, it } from 'vitest'
import { describeReplay } from '@truewire/testing/replay'
import { withClient } from './client.js'
import { projectRoot } from './setup.js'

describeReplay({
  test: { describe, it },
  projectRoot,
  packageDir: path.join(projectRoot, 'src', 'kraken'),
  withClient,
  importModule: url => import(/* @vite-ignore */ url),
  include: example => !(example.transport === 'ws' && example.endpoint.kind === 'stream' && example.reply === undefined),
})
