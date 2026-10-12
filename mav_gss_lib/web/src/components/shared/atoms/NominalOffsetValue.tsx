import { colors } from '@/lib/colors'
import { formatKhzOffset, hasNominalOffset, nominalOffsetRows } from '@/lib/frequencyOffset'
import type { SignalMeasurement } from '@/lib/types'
import { BreakdownTooltip } from './BreakdownTooltip'

/** RX Δf nom cell: the beacon's offset from nominal, with the Doppler /
 *  residual split on hover. "--" when the frame has no measurement (during
 *  TX, undecoded burst, or a log recorded before tune logging). */
export function NominalOffsetValue({ signal }: { signal?: SignalMeasurement }) {
  if (!signal || !hasNominalOffset(signal)) {
    return <span style={{ color: colors.textDisabled }}>--</span>
  }
  return (
    <BreakdownTooltip rows={nominalOffsetRows(signal)}>
      <span className="tabular-nums" style={{ color: colors.dim }}>{formatKhzOffset(signal.nominal_offset_hz!)}</span>
    </BreakdownTooltip>
  )
}
