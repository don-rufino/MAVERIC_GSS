import { useRadioStatus } from '@/state/radioHooks'
import { useTrackingStatus } from '@/state/trackingHooks'
import { colors } from '@/lib/colors'
import type { RadioStatus } from '@/components/radio/useRadioSocket'
import type { DopplerMode } from '@/lib/types'

interface HeaderStatusPillsProps {
  // Both pills currently route to the Radio tab — Tracking lives there
  // via DopplerSection. If a dedicated Tracking tab is added later,
  // split this into per-pill callbacks.
  onPillClick: () => void
}

interface PillTone {
  dot: string
  text: string
  title: string
  ariaLabel: string
}

function radioTone(status: RadioStatus): PillTone {
  if (!status.enabled) {
    return {
      dot: colors.dim,
      text: colors.textMuted,
      title: 'radio: disabled in config',
      ariaLabel: 'radio disabled',
    }
  }
  switch (status.state) {
    case 'running':
      return {
        dot: colors.success,
        text: colors.textSecondary,
        title: `radio: running${status.pid ? ` (pid ${status.pid})` : ''}`,
        ariaLabel: 'radio running',
      }
    case 'stopping':
      return {
        dot: colors.warning,
        text: colors.textSecondary,
        title: 'radio: stopping',
        ariaLabel: 'radio stopping',
      }
    case 'crashed':
      return {
        dot: colors.danger,
        text: colors.textSecondary,
        title: status.error ? `radio: crashed — ${status.error}` : 'radio: crashed',
        ariaLabel: 'radio crashed',
      }
    default:
      return {
        dot: colors.dim,
        text: colors.textMuted,
        title: 'radio: stopped',
        ariaLabel: 'radio stopped',
      }
  }
}

function dopplerTone(mode: DopplerMode, error: string): PillTone {
  if (mode === 'connected') {
    return {
      dot: colors.success,
      text: colors.textSecondary,
      title: 'doppler: engaged',
      ariaLabel: 'doppler engaged',
    }
  }
  if (mode === 'static') {
    return {
      dot: colors.warning,
      text: colors.textSecondary,
      title: 'doppler: static mode',
      ariaLabel: 'doppler static mode',
    }
  }
  return {
    dot: colors.dim,
    text: colors.textMuted,
    title: error ? `doppler: disengaged — ${error}` : 'doppler: disengaged',
    ariaLabel: 'doppler disengaged',
  }
}

function Pill({
  tone,
  label,
  onClick,
}: {
  tone: PillTone
  label: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={tone.title}
      aria-label={tone.ariaLabel}
      className="flex items-center gap-1.5 px-1.5 py-0.5 rounded hover:bg-white/[0.04] transition-colors"
      style={{ color: tone.text, fontFamily: "'Inter', sans-serif" }}
    >
      <span
        aria-hidden
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          backgroundColor: tone.dot,
          boxShadow: `0 0 4px ${tone.dot}66`,
          transition: 'background-color 250ms ease, box-shadow 250ms ease',
        }}
      />
      <span style={{ fontSize: '11px', letterSpacing: '0.06em', textTransform: 'uppercase' }}>{label}</span>
    </button>
  )
}

export function HeaderStatusPills({ onPillClick }: HeaderStatusPillsProps) {
  const radio = useRadioStatus()
  const tracking = useTrackingStatus()
  return (
    <div className="flex items-center gap-0.5">
      <Pill tone={radioTone(radio.status)} label="RADIO" onClick={onPillClick} />
      <Pill tone={dopplerTone(tracking.mode, tracking.error)} label="DOPPLER" onClick={onPillClick} />
    </div>
  )
}
