/**
 * Real, representative usage of the generated client, type-checked by `yarn typecheck`
 * and never run: the guardrail against a public return type silently degrading, and the
 * proof that `{ validate: false }` is typed as what it returns.
 *
 * `expectTypeOf` is vitest's compile-time assertion; a mismatch fails `tsc`.
 */
import type { CallOptions, PaginatedResponse } from '@truewire/core'
import { expectTypeOf } from 'vitest'
import { Core } from '../src/github/core/index.js'
import { GitHub } from '../src/github/index.js'
import type { Issue } from '../src/github/issues/list.js'
import type { Repository } from '../src/github/repos/get.js'
import { ListCommits, type Commits } from '../src/github/repos/list_commits.js'
import type { Commit } from '../src/github/types/index.js'

const where = { owner: 'truewire-dev', repo: 'truewire' }

/** The parsed value: by default, with `validate: true`, and with a flag decided elsewhere. */
export async function validatedByDefault(strict: boolean, options: CallOptions): Promise<void> {
  const client = new GitHub(new Core())
  const repo = await client.repos.get(where)
  expectTypeOf(repo).toEqualTypeOf<Repository>()
  expectTypeOf(repo.created_at).toEqualTypeOf<Date>()
  expectTypeOf(await client.repos.get(where, { validate: true })).toEqualTypeOf<Repository>()
  expectTypeOf(await client.repos.get(where, { validate: strict })).toEqualTypeOf<Repository>()
  expectTypeOf(await client.repos.get(where, options)).toEqualTypeOf<Repository>()
  expectTypeOf(await client.repos.listCommits({ ...where, per_page: 3 })).toEqualTypeOf<Commits>()
  expectTypeOf(client.repos.listCommitsPaged({ ...where, per_page: 3 })).toEqualTypeOf<PaginatedResponse<Commit, number>>()
  expectTypeOf(client.issues.listPaged({ ...where, state: 'all' })).toEqualTypeOf<PaginatedResponse<Issue, number>>()
}

/** `{ validate: false }` returns the body as the wire sent it, and says so: `unknown`. */
export async function rawBodies(): Promise<void> {
  const core = new Core()
  const client = new GitHub(core)
  expectTypeOf(await client.repos.get(where, { validate: false })).toBeUnknown()
  expectTypeOf(await client.repos.listCommits(where, { validate: false })).toBeUnknown()
  expectTypeOf(client.repos.listCommitsPaged(where, { validate: false })).toEqualTypeOf<PaginatedResponse<unknown, number>>()
  expectTypeOf(client.issues.listPaged(where, { validate: false })).toEqualTypeOf<PaginatedResponse<unknown, number>>()
  // The overloads live on the endpoint class; the router delegates them.
  expectTypeOf(await new ListCommits(core).listCommits(where, { validate: false })).toBeUnknown()
  const raw = { validate: false } as const
  expectTypeOf(await client.repos.get(where, raw)).toBeUnknown()
}
