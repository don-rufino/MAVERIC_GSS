import { describe, expect, it } from 'vitest'
import { bandwidthLevels, floorNote, marginTone, signed } from './signal'
import type { SignalMeasurement } from './types'
import scionx from '@/components/rx/__fixtures__/scionx_20260923_signal.json'

const sig = { v: 1, status: 'ok' } as SignalMeasurement

describe('marginTone', () => {
  it('splits at 0 and 3 dB of margin', () => {
    expect(marginTone(19.7, 7.8)).toBe('success')
    expect(marginTone(8.5, 7.8)).toBe('warning')
    expect(marginTone(7.0, 7.8)).toBe('danger')
  })
})

describe('signed', () => {
  it('uses a real minus sign', () => {
    expect(signed(-3027)).toBe('−3027')
    expect(signed(2051)).toBe('+2051')
    expect(signed(0)).toBe('0')
  })
})

describe('floorNote', () => {
  it('is silent for the normal pre-burst floor', () => {
    expect(floorNote({ ...sig, floor_method: 'pre_burst' })).toBeNull()
  })
  it('explains floors moved by an uplink', () => {
    expect(floorNote({ ...sig, floor_method: 'pre_burst_earlier' })?.label).toBe('FLOOR: EARLIER')
    expect(floorNote({ ...sig, floor_method: 'beside_signal', floor_tx_affected: true })?.detail).toMatch(/uplink/)
  })
})

describe('bandwidthLevels', () => {
  const beacons = (scionx.beacons as unknown as SignalMeasurement[])

  it('uses the logged levels when the meter recorded them', () => {
    const r = bandwidthLevels(beacons[0])!
    expect(r.fromSpectrum).toBe(false)
    expect(r.levels).toBe(beacons[0].bw_levels)
  })

  it('recalculates from the saved spectrum exactly as gnuradio/signal_meter.py logged it', () => {
    for (const b of beacons) {
      const { bw_levels, ...older } = b
      const r = bandwidthLevels(older)!
      expect(r.fromSpectrum).toBe(true)
      expect(r.levels).toEqual(bw_levels)
    }
  })

  it('marks a level too close to the floor as near floor', () => {
    const { bw_levels: _, ...older } = beacons[0]
    const weak = { ...older, floor_dbfs: older.peak_dbfs! - 21 }   // -20 dB line 1 dB up
    const r = bandwidthLevels(weak)!
    expect(r.levels['20']).toEqual({ status: 'near_floor', line_above_floor_db: 1 })
    expect(r.levels['10'].status).toBe('ok')
  })

  it('returns null without a saved spectrum', () => {
    expect(bandwidthLevels({ v: 1, status: 'during_tx' })).toBeNull()
  })
})
