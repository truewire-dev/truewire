/**
 * Generic replay coverage through `@truewire/testing`: every recorded HTTP example, through
 * the real client against the mock.
 *
 * Each `spec/endpoints/**\/examples/<id>.request.json` is replayed by calling the method
 * the endpoint's function path names (`repos.list_commits` is `client.repos.listCommits`),
 * with the recorded request parsed through the endpoint's `Request` codec. Validation is
 * on, so the test proves the codec accepts the recorded response; a second call with
 * `validate: false` proves the raw reply has the same shape.
 */
import path from 'node:path'
import { describe, inject, it } from 'vitest'
import { describeHttpReplay } from '@truewire/testing/replay'
import { GitHub } from '../src/github/index.js'
import { Core } from '../src/github/core/index.js'
import { projectRoot } from './setup.js'

describeHttpReplay({
  test: { describe, it },
  projectRoot,
  packageDir: path.join(projectRoot, 'src', 'github'),
  importModule: url => import(/* @vite-ignore */ url),
  withClient: body => body(new GitHub(new Core({ baseUrl: inject('httpBaseUrl') }))),
})
