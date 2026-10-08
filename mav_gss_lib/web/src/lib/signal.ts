import type { SignalMeasurement } from '@/lib/types'

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
