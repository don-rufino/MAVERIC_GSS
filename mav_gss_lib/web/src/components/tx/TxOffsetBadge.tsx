import { colors } from '@/lib/colors'

export interface TxOffsetBadgeProps {
  /** Hz, or undefined for a row that hasn't been sent yet (offset isn't
   *  known until send time). 0 is a real, common value (sweep off, or
   *  currently centered on its base) — shown muted rather than hidden, so
   *  it doesn't read as "unknown". */
  offsetHz: number | undefined
}

export function TxOffsetBadge({ offsetHz }: TxOffsetBadgeProps) {
  if (offsetHz == null) {
    return <span className="font-mono text-[11px]" style={{ color: colors.textDisabled }}>--</span>
  }
  const tone = offsetHz === 0 ? colors.textMuted : colors.warning
  const sign = offsetHz > 0 ? '+' : ''
  return (
    <span className="font-mono text-[11px] tabular-nums" style={{ color: tone }}>
      {sign}{(offsetHz / 1000).toFixed(1)} kHz
    </span>
  )
}
