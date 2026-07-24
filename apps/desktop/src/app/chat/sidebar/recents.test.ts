import { describe, expect, it } from 'vitest'

import { olderHalf, olderHalfBy } from './recents'

describe('olderHalf', () => {
  it('returns the trailing half of an even newest-first list', () => {
    expect(olderHalf(['newest', 'newer', 'older', 'oldest'])).toEqual(['older', 'oldest'])
  })

  it('keeps the larger half when the list length is odd', () => {
    expect(olderHalf(['newest', 'middle', 'oldest'])).toEqual(['oldest'])
  })

  it('uses recency instead of display order when selecting older items', () => {
    const sessions = [
      { id: 'oldest', time: 1 },
      { id: 'newest', time: 4 },
      { id: 'older', time: 2 },
      { id: 'newer', time: 3 }
    ]

    expect(olderHalfBy(sessions, session => session.time).map(session => session.id)).toEqual(['older', 'oldest'])
  })

  it('returns nothing when fewer than two recents exist', () => {
    expect(olderHalf([])).toEqual([])
    expect(olderHalf(['only'])).toEqual([])
  })
})
