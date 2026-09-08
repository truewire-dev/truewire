/**
 * The generated codecs against the recordings themselves, without the mock: `parse`
 * turns a recorded wire body into the typed value (timestamps become `Date`s) and `dump`
 * renders it back to exactly the body that was recorded.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { ValidationError } from '@truewire/core'
import { Repository } from '../src/github/repos/get.js'
import { Commits } from '../src/github/repos/list_commits.js'
import { projectRoot } from './setup.js'

const recorded = (endpoint: string, id: string): unknown =>
  (JSON.parse(readFileSync(path.join(projectRoot, 'spec', 'endpoints', endpoint, 'examples', `${id}.response.json`), 'utf8')) as { payload: unknown }).payload

describe('generated codecs', () => {
  it('parse a recorded repository and dump it back verbatim', () => {
    const payload = recorded('repos/get', 'default')
    const repository = Repository.parse(payload)
    expect(repository.full_name).toBe('truewire-dev/truewire')
    expect(repository.created_at).toBeInstanceOf(Date)
    expect(Repository.dump(repository)).toEqual(payload)
  })

  it('parse a recorded page of commits and dump it back verbatim', () => {
    const payload = recorded('repos/list_commits', 'page1')
    const commits = Commits.parse(payload)
    expect(commits).toHaveLength(3)
    expect(commits[0]!.commit.author.date.toISOString()).toMatch(/Z$/)
    expect(Commits.dump(commits)).toEqual(payload)
  })

  it('name the path of a field that does not match', () => {
    const payload = recorded('repos/get', 'default') as Record<string, unknown>
    expect(() => Repository.parse({ ...payload, created_at: 'yesterday' })).toThrow(ValidationError)
    try {
      Repository.parse({ ...payload, owner: { ...(payload.owner as object), id: 'x' } })
    } catch (e) {
      expect((e as ValidationError).path).toBe('/owner/id')
    }
  })
})
