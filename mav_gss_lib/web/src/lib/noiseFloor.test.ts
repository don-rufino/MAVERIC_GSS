import { describe, expect, it } from 'vitest'
import fixture from '@/components/radio/__fixtures__/noise_floor_pass.json'
import {
  ROW, STATE, gainMarkers, heldSeconds, mergeLive, passLabel, recentQuiet, spansOf,
  type NoiseFloorPass, type SeriesRow,
} from './noiseFloor'

const complete = fixture.complete as unknown as NoiseFloorPass
const liveAdjusting = fixture.live_adjusting as unknown as NoiseFloorPass
const series = complete.series as SeriesRow[]

describe('mergeLive', () => {
  const upTo = (n: number): NoiseFloorPass => ({ ...complete, seconds: n, series: series.slice(0, n) })

  it('appends the tail and lets its rows replace older states', () => {
    const active = upTo(100)
    const revised = series.slice(96, 101).map(r => [...r]) as SeriesRow[]
    revised[0][ROW.state] = STATE.burst
    const next = mergeLive(active, { ...complete, seconds: 101, series: undefined }, revised)!
    expect(next.series).toHaveLength(101)
    expect(next.series![96][ROW.state]).toBe(STATE.burst)
    expect(next.series![100]).toEqual(series[100])
  })

  it('starts a new pass from its first rows', () => {
    const next = mergeLive(upTo(100), { ...complete, aos_ms: complete.aos_ms + 6e6, seconds: 3 }, series.slice(0, 3))!
    expect(next.series).toHaveLength(3)
  })

  it('asks for a refetch when joining mid-pass or when rows went missing', () => {
    expect(mergeLive(null, { ...complete, seconds: 120 }, series.slice(114, 120))).toBeNull()
    expect(mergeLive(upTo(100), { ...complete, seconds: 110 }, series.slice(104, 110))).toBeNull()
  })
})

describe('series helpers', () => {
  it('marks where each counted gain began', () => {
    expect(gainMarkers(series)).toEqual([{ t: 0, gain: 27 }, { t: 183, gain: 33 }])
  })

  it('finds the adjusting and held-at-55 stretches', () => {
    expect(spansOf(series, r => r[ROW.state] === STATE.adjusting)).toEqual([[180, 182]])
    const held = spansOf(series, r => r[ROW.satEl] > complete.el_max_deg)
    expect(held).toHaveLength(1)
    // the series rounds elevation to 0.1°, so a second at 55.04° reads 55.0° there
    expect(held[0][1] - held[0][0] + 1).toBeGreaterThanOrEqual(complete.held_at_max_s - 2)
    expect(held[0][1] - held[0][0] + 1).toBeLessThanOrEqual(complete.held_at_max_s)
  })

  it('reports how long the current gain has held', () => {
    expect(heldSeconds(liveAdjusting.series as SeriesRow[])).toBe(0)
    expect(heldSeconds(series)).toBeGreaterThan(400)
  })

  it('averages the last quiet seconds at the current gain', () => {
    const last10 = series.filter(r => r[ROW.state] === STATE.quiet && r[ROW.gain] === 33).slice(-10).map(r => r[ROW.floor]!)
    const expected = 10 * Math.log10(last10.reduce((acc, v) => acc + 10 ** (v / 10), 0) / 10)
    expect(recentQuiet(series, 33)).toBeCloseTo(expected, 6)
    expect(recentQuiet(series, 40)).toBeNull()
  })

  it('labels a pass by its UTC AOS', () => {
    expect(passLabel(Date.UTC(2026, 9, 16, 18, 42, 30))).toBe('10-16 18:42Z')
  })
})
