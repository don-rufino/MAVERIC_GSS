import { signed } from '@/lib/signal'
import type { SignalMeasurement } from '@/lib/types'

/** "+1.5 kHz" / "-10.2 kHz" / "0.0 kHz": the format shared by the TX
 *  Δf sweep and RX Δf nom columns, live and in the Log Viewer. */
export function formatKhzOffset(hz: number): string {
  const khz = Math.round(hz / 100) / 10
  if (khz === 0) return '0.0 kHz'
  return `${khz > 0 ? '+' : ''}${khz.toFixed(1)} kHz`
}

/** "437.508 259" (MHz, Hz resolution, grouped for reading). */
export function formatMhz(hz: number): string {
  const s = (hz / 1e6).toFixed(6)
  return `${s.slice(0, -3)} ${s.slice(-3)}`
}

export interface BreakdownRow {
  label: string
  value: string
  /** Indented under the row above (a component of it). */
  sub?: boolean
}

/** A beacon's offset from nominal, split into the Doppler correction the
 *  station applied and the residual it didn't (signal meter `tune` block). */
export function nominalOffsetRows(s: SignalMeasurement): BreakdownRow[] {
  const tune = s.tune!
  const doppler = tune.tuned_hz - tune.nominal_hz
  return [
    { label: 'Offset from nominal', value: `${signed(s.nominal_offset_hz!)} Hz` },
    { label: 'Received center', value: `${formatMhz(s.received_center_hz!)} MHz` },
    { label: 'Doppler correction', value: tune.doppler ? `${signed(doppler)} Hz` : 'off', sub: true },
    { label: 'Residual (vs tuned)', value: `${signed(s.center_offset_hz!)} Hz`, sub: true },
  ]
}

export function hasNominalOffset(s: SignalMeasurement | undefined): boolean {
  return !!s && !!s.tune && typeof s.nominal_offset_hz === 'number'
}

export const SWEEP_STEP_NOTE = 'Added on top of the Doppler-corrected TX frequency'
