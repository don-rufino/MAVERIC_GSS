import { colors } from '@/lib/colors'
import { formatKhzOffset, SWEEP_STEP_NOTE } from '@/lib/frequencyOffset'
import { signed } from '@/lib/signal'
import { BreakdownTooltip } from '@/components/shared/atoms/BreakdownTooltip'

export interface TxOffsetBadgeProps {
  /** Hz, or undefined for a row that hasn't been sent yet (offset isn't
   *  known until send time). 0 is a real, common value (sweep off, or
   *  currently centered on its base) — shown muted rather than hidden, so
   *  it doesn't read as "unknown". */
  offsetHz: number | undefined
  /** Where the value came from, for the hover breakdown (Log Viewer). */
  provenance?: string
}

/** TX Δf sweep cell: the offset-sweep step this command carried, which is
 *  relative to the Doppler-corrected frequency, not to nominal. */
export function TxOffsetBadge({ offsetHz, provenance }: TxOffsetBadgeProps) {
  if (offsetHz == null) {
    return <span className="font-mono text-[11px]" style={{ color: colors.textDisabled }}>--</span>
  }
  const tone = offsetHz === 0 ? colors.textMuted : colors.warning
  return (
    <BreakdownTooltip
      rows={[{ label: 'Sweep step', value: `${signed(offsetHz)} Hz` }]}
      note={provenance ? `${SWEEP_STEP_NOTE} · ${provenance}` : SWEEP_STEP_NOTE}
    >
      <span className="font-mono text-[11px] tabular-nums" style={{ color: tone }}>
        {formatKhzOffset(offsetHz)}
      </span>
    </BreakdownTooltip>
  )
}
