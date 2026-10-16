import type { BandwidthLevel, SignalMeasurement } from '@/lib/types'

export function isMeasured(signal: SignalMeasurement | undefined): signal is SignalMeasurement {
  return !!signal && signal.status === 'ok' && !!signal.spectrum
}

/** Signed number with a real minus sign, e.g. "+2051" / "−3027". */
export function signed(v: number, digits = 0): string {
  const sign = v > 0 ? '+' : v < 0 ? '−' : ''
  return `${sign}${Math.abs(v).toFixed(digits)}`
}

export type MarginTone = 'success' | 'warning' | 'danger'

/** >= 3 dB above required is nominal, 0-3 dB marginal, below required danger. */
export function marginTone(ebn0: number, required: number): MarginTone {
  const margin = ebn0 - required
  if (margin >= 3) return 'success'
  if (margin >= 0) return 'warning'
  return 'danger'
}

export const MARGIN_LABEL: Record<MarginTone, string> = {
  success: 'MARGIN OK',
  warning: 'MARGINAL',
  danger: 'BELOW REQUIRED',
}

/** Operator-facing note when the floor was not read from the clean second
 *  right before the beacon. */
export function floorNote(signal: SignalMeasurement): { label: string; detail: string } | null {
  if (signal.floor_method === 'pre_burst_earlier') {
    return {
      label: 'FLOOR: EARLIER',
      detail: 'The second before this beacon overlapped an uplink, so the floor was read from clean rows further back.',
    }
  }
  if (signal.floor_method === 'beside_signal') {
    return {
      label: 'FLOOR: BESIDE SIGNAL',
      detail: signal.floor_tx_affected
        ? 'No clean rows before this beacon (uplink), so the floor was read from bins beside the signal.'
        : 'No clean rows before this beacon, so the floor was read from bins beside the signal.',
    }
  }
  return null
}

/** Mirror gnuradio/signal_meter.py BW_LEVELS_DB and NEAR_FLOOR_GUARD_DB. */
export const BW_LEVELS_DB = [10, 20] as const
export const NEAR_FLOOR_GUARD_DB = 3

/** The −10/−20 dB levels as logged, or, for beacons logged before the meter
 *  recorded them, recalculated from the saved spectrum the way the meter does:
 *  walk outward from the −3 dB outer edges until the spectrum drops below
 *  peak − N dB. The saved spectrum is rounded to 0.1 dB, so an edge can
 *  rarely land one bin off what the live meter would have recorded. */
export function bandwidthLevels(
  s: SignalMeasurement,
): { levels: Record<string, BandwidthLevel>; fromSpectrum: boolean } | null {
  if (s.bw_levels) return { levels: s.bw_levels, fromSpectrum: false }
  const spec = s.spectrum
  if (!spec || !s.edges_3db_hz || typeof s.floor_dbfs !== 'number') return null
  const dbs = spec.db10.map(v => v / 10)
  const last = dbs.length - 1
  const freqAt = (k: number) => spec.f0_hz + k * spec.df_hz
  const binAt = (hz: number) => Math.min(last, Math.max(0, Math.round((hz - spec.f0_hz) / spec.df_hz)))
  const peak = Math.max(...dbs)
  const levels: Record<string, BandwidthLevel> = {}
  for (const levelDb of BW_LEVELS_DB) {
    const line = peak - levelDb
    const aboveFloor = Math.round((line - s.floor_dbfs) * 10) / 10
    if (aboveFloor < NEAR_FLOOR_GUARD_DB) {
      levels[levelDb] = { status: 'near_floor', line_above_floor_db: aboveFloor }
      continue
    }
    let lo = binAt(s.edges_3db_hz[0])
    let hi = binAt(s.edges_3db_hz[1])
    while (lo > 0 && dbs[lo - 1] >= line) lo--
    while (hi < last && dbs[hi + 1] >= line) hi++
    levels[levelDb] = {
      status: 'ok',
      edges_hz: [Math.round(freqAt(lo)), Math.round(freqAt(hi))],
      bw_hz: Math.round(freqAt(hi) - freqAt(lo) + spec.df_hz),
      line_above_floor_db: aboveFloor,
      band_limited: lo === 0 || hi === last,
    }
  }
  return { levels, fromSpectrum: true }
}
