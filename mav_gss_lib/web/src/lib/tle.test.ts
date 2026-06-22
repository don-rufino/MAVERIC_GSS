import { describe, it, expect } from 'vitest'
import { parseTleBlock, joinTleBlock } from './tle'

describe('parseTleBlock', () => {
  it('parses a full three-line Celestrak block (name + two element lines)', () => {
    const text = [
      'MAVERIC',
      '1 99999U 26001A   26182.53800926  .00000000  00000-0  15000-3 0  9999',
      '2 99999  97.8250 154.7171 0058009 348.1000 351.9980 14.91466332000019',
    ].join('\n')
    expect(parseTleBlock(text)).toEqual({
      name: 'MAVERIC',
      line1: '1 99999U 26001A   26182.53800926  .00000000  00000-0  15000-3 0  9999',
      line2: '2 99999  97.8250 154.7171 0058009 348.1000 351.9980 14.91466332000019',
    })
  })

  it('parses a two-line block with no name line', () => {
    const text = [
      '1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990',
      '2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007',
    ].join('\n')
    expect(parseTleBlock(text)).toEqual({
      line1: '1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990',
      line2: '2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007',
    })
  })

  it('trims whitespace and ignores blank lines', () => {
    const text = '\n  MAVERIC  \n\n  1 99999U 26001A   26182.5 0  9999  \n 2 99999  97.8 14.9 19 \n'
    expect(parseTleBlock(text)).toEqual({
      name: 'MAVERIC',
      line1: '1 99999U 26001A   26182.5 0  9999',
      line2: '2 99999  97.8 14.9 19',
    })
  })

  it('is order-independent (name after element lines)', () => {
    const text = [
      '1 99999U 26001A   26182.5 0  9999',
      '2 99999  97.8 14.9 19',
      'MAVERIC',
    ].join('\n')
    expect(parseTleBlock(text)).toEqual({
      name: 'MAVERIC',
      line1: '1 99999U 26001A   26182.5 0  9999',
      line2: '2 99999  97.8 14.9 19',
    })
  })

  it('does not mistake a name starting with a digit for an element line', () => {
    const text = ['1RABBIT', '1 99999U ... 9999', '2 99999  ... 19'].join('\n')
    expect(parseTleBlock(text)).toEqual({
      name: '1RABBIT',
      line1: '1 99999U ... 9999',
      line2: '2 99999  ... 19',
    })
  })

  it('returns an empty object for empty input', () => {
    expect(parseTleBlock('')).toEqual({})
    expect(parseTleBlock('   \n  \n')).toEqual({})
  })

  it('keeps the first occurrence when lines are duplicated', () => {
    const text = ['1 AAA', '1 BBB', '2 CCC'].join('\n')
    expect(parseTleBlock(text)).toEqual({ line1: '1 AAA', line2: '2 CCC' })
  })
})

describe('joinTleBlock', () => {
  it('joins name + lines into a pasteable block', () => {
    expect(joinTleBlock({ name: 'MAVERIC', line1: '1 AAA', line2: '2 BBB' })).toBe('MAVERIC\n1 AAA\n2 BBB')
  })

  it('omits absent fields', () => {
    expect(joinTleBlock({ line1: '1 AAA', line2: '2 BBB' })).toBe('1 AAA\n2 BBB')
    expect(joinTleBlock({})).toBe('')
  })

  it('round-trips with parseTleBlock', () => {
    const parsed = { name: 'MAVERIC', line1: '1 AAA', line2: '2 BBB' }
    expect(parseTleBlock(joinTleBlock(parsed))).toEqual(parsed)
  })
})
