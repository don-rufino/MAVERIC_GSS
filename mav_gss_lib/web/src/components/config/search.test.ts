import { describe, it, expect } from 'vitest'
import { matchesQuery } from './search'

describe('matchesQuery', () => {
  it('matches everything when the query is blank', () => {
    expect(matchesQuery('', 'RX frequency')).toBe(true)
    expect(matchesQuery('   ', 'anything', 'here')).toBe(true)
  })
  it('is a case-insensitive substring match', () => {
    expect(matchesQuery('freq', 'RX Frequency', 'Downlink center')).toBe(true)
    expect(matchesQuery('FREQ', 'RX frequency')).toBe(true)
  })
  it('requires every whitespace-separated term (AND)', () => {
    expect(matchesQuery('tx delay', 'TX delay', 'Pause before uplink')).toBe(true)
    expect(matchesQuery('tx blackout', 'TX delay')).toBe(false)
  })
  it('searches across description text, not just label', () => {
    expect(matchesQuery('uplink', 'TX delay', 'Pause before each uplink frame')).toBe(true)
  })
  it('returns false when nothing matches', () => {
    expect(matchesQuery('zzz', 'RX frequency', 'Downlink')).toBe(false)
  })
  it('ignores undefined haystack entries', () => {
    expect(matchesQuery('rx', 'RX frequency', undefined)).toBe(true)
  })
})
