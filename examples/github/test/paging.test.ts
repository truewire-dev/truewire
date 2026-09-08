/**
 * The generated `<method>Paged` walkers, driven by the recorded multi-page captures.
 *
 * Each walk below replays a real sequence of pages recorded from api.github.com. The mock
 * serves page N only for the exact `page`/`per_page` the walk sends, so a walker that
 * mis-computed the next index would get a 422, not a quietly wrong result. The same walks
 * as `test/test_paging.py`, through the TypeScript client.
 */
import { describe, expect, inject, it } from 'vitest'
import { GitHub } from '../src/github/index.js'
import { Core } from '../src/github/core/index.js'

/** The walks start from a fixed commit so re-recording them yields the same pages. */
const RELEASE_0_1_0 = '934a718509b0cfd1244db90821201afbbb728797'

const client = () => new GitHub(new Core({ baseUrl: inject('httpBaseUrl') }))

describe('repos.listCommitsPaged', () => {
  it('ends on the short fourth page', async () => {
    // Eleven commits at three per page: three full pages, then a page of two.
    const pages = []
    for await (const page of client().repos.listCommitsPaged({ owner: 'truewire-dev', repo: 'truewire', sha: RELEASE_0_1_0, per_page: 3 })) {
      pages.push(page)
    }
    expect(pages.map(page => page.length)).toEqual([3, 3, 3, 2])
    const shas = pages.flat().map(commit => commit.sha)
    expect(new Set(shas).size).toBe(11)
    for (const commit of pages.flat()) {
      expect(commit.commit.author.date).toBeInstanceOf(Date)
      expect(Number.isNaN(commit.commit.author.date.getTime())).toBe(false)
    }
  })

  it('flattens when awaited', async () => {
    const commits = await client().repos.listCommitsPaged({ owner: 'truewire-dev', repo: 'truewire', sha: RELEASE_0_1_0, per_page: 3 })
    expect(commits).toHaveLength(11)
    expect(commits[0]!.commit.message.startsWith('Release truewire 0.1.0')).toBe(true)
  })
})

describe('issues.listPaged', () => {
  it('ends on an empty page', async () => {
    // Merged pull requests at one per page, newest first, until an empty page ends the
    // walk. The count moves with the repository (every release adds a pull request), so
    // the assertions are about the walk, not the number.
    const issues = await client().issues.listPaged({ owner: 'truewire-dev', repo: 'truewire', state: 'all', per_page: 1 })
    const numbers = issues.map(issue => issue.number)
    expect(numbers.length).toBeGreaterThan(0)
    expect(numbers).toEqual([...numbers].sort((a, b) => b - a))
    for (const issue of issues) {
      expect(issue.pull_request?.merged_at).toBeInstanceOf(Date)
      expect(issue.state).toBe('closed')
    }
  })
})
