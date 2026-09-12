import type { ReactNode } from 'react'
import { Satellite } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { colors } from '@/lib/colors'
import { cn } from '@/lib/utils'
import type { DopplerCorrection, DopplerMode } from '@/lib/types'

export interface DopplerSectionProps {
  doppler: DopplerCorrection | null
  mode: DopplerMode
  error: string
  busy: 'engage' | 'disengage' | 'static-on' | 'static-off' | null
  actionError: string | null
  engage: () => Promise<void>
  disengage: () => Promise<void>
  toggleStatic: () => Promise<void>
  dismissError: () => void
}

function fmtHz(hz: number): string {
  return Math.round(hz).toLocaleString('en-US')
}

function fmtSigned(value: number, digits = 1): string {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(digits)}`
}

const GPREDICT_DEFAULT_HZ = 100_000_000

// GPredict shows a single "Signal loss" figure at 100 MHz whenever a
// satellite has no .trsp transponder entry (the common case for an
// unidentified rideshare candidate) instead of the real downlink
// frequency. Same range, so the two loss figures differ by a fixed
// 20*log10(f_ratio) — reusing the already-computed loss avoids needing
// range_km on the wire just for this comparison value.
function lossAtFreq(actualLossDb: number, actualHz: number, targetHz: number): number {
  return actualLossDb + 20 * Math.log10(targetHz / actualHz)
}

function PanelHeader({ icon, title, right }: { icon: ReactNode; title: string; right?: ReactNode }) {
  return (
    <div
      className="flex min-h-[33px] shrink-0 items-center justify-between gap-3 border-b px-3 py-1.5"
      style={{ borderColor: colors.borderSubtle }}
    >
      <div className="flex min-w-0 items-center gap-2">
        {icon}
        <span className="truncate text-xs font-bold uppercase tracking-wide" style={{ color: colors.value }}>
          {title}
        </span>
      </div>
      {right}
    </div>
  )
}

function DataCell({ label, value, tone, className }: { label: string; value: string; tone?: string; className?: string }) {
  return (
    <div className={cn('min-w-0 py-1', className)}>
      <div className="text-[11px] font-medium uppercase" style={{ color: colors.textMuted }}>{label}</div>
      <div title={value} className="mt-0.5 truncate font-mono text-xs" style={{ color: tone ?? colors.textPrimary }}>
        {value}
      </div>
    </div>
  )
}

export function DopplerSection(props: DopplerSectionProps) {
  const { doppler, mode, error, busy, actionError, engage, disengage, toggleStatic } = props
  const engaged = mode === 'connected'
  const staticOn = mode === 'static'
  const tone = error
    ? colors.danger
    : staticOn ? colors.warning
    : engaged ? colors.success : colors.textMuted
  const label = error ? 'ERROR' : staticOn ? 'STATIC' : engaged ? 'ENGAGED' : 'DISENGAGED'

  const onClick = engaged ? disengage : engage
  const buttonLabel = busy === 'engage' ? 'Engaging…'
    : busy === 'disengage' ? 'Disengaging…'
    : engaged ? 'Disengage' : 'Engage'

  return (
    <section
      className="flex flex-col rounded-lg border shadow-panel"
      style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}
    >
      <PanelHeader
        icon={<Satellite className="size-3.5 shrink-0" style={{ color: colors.dim }} />}
        title="Doppler"
        right={(
          <Badge
            variant="outline"
            className="h-5 rounded text-[11px]"
            style={{ color: tone, borderColor: `${tone}66`, backgroundColor: 'transparent' }}
          >
            {label}
          </Badge>
        )}
      />
      <div className="flex flex-col gap-2 px-3 py-2">
        <div
          className="flex items-start justify-between gap-2.5 rounded-md border px-2.5 py-2"
          style={{
            borderColor: staticOn ? `${colors.warning}66` : colors.borderSubtle,
            backgroundColor: staticOn ? `${colors.warning}14` : colors.bgCard,
          }}
        >
          <div className="min-w-0">
            <div className="text-xs font-bold" style={{ color: colors.textPrimary }}>Static Mode</div>
            <div className="mt-0.5 text-[11px] leading-snug" style={{ color: staticOn ? colors.warning : colors.textMuted }}>
              {engaged
                ? 'Disengage Doppler to enable.'
                : 'Hold RX/TX at nominal frequency; stop background Doppler calc.'}
            </div>
          </div>
          <Switch
            checked={staticOn}
            onCheckedChange={() => void toggleStatic()}
            disabled={busy !== null || engaged}
            aria-label="Static Mode"
            title={engaged ? 'Disengage Doppler to enable Static Mode' : undefined}
          />
        </div>

        <Button
          size="sm"
          variant="outline"
          aria-busy={busy !== null}
          onClick={() => void onClick()}
          disabled={busy !== null || staticOn}
          title={staticOn ? 'Turn off Static Mode to engage' : undefined}
          className="h-8 w-full gap-1.5 text-xs font-bold btn-feedback"
          style={{
            color: engaged ? colors.danger : colors.active,
            borderColor: engaged ? `${colors.danger}66` : `${colors.active}66`,
            backgroundColor: engaged ? `${colors.danger}08` : `${colors.active}08`,
          }}
        >
          {buttonLabel}
        </Button>

        <div className="grid grid-cols-3 gap-x-4 gap-y-1">
          <DataCell label="Satellite" value={staticOn ? '--' : (doppler?.satellite ?? '--')} />
          <DataCell label="Mode" value={mode.toUpperCase()} tone={tone} />
          <DataCell
            label="Range Rate"
            value={!staticOn && doppler ? `${fmtSigned(doppler.range_rate_mps, 1)} m/s` : '--'}
          />
        </div>

        {staticOn ? (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            <DataCell label="RX Parked At" value={doppler ? `${fmtHz(doppler.rx_hz)} Hz` : '--'} />
            <DataCell label="TX Parked At" value={doppler ? `${fmtHz(doppler.tx_hz)} Hz` : '--'} />
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            <DataCell label="RX Shift" value={doppler ? `${fmtSigned(doppler.rx_shift_hz, 0)} Hz` : '--'} />
            <DataCell label="RX Tune"  value={doppler ? `${fmtHz(doppler.rx_tune_hz)} Hz` : '--'} />
            <DataCell label="TX Shift" value={doppler ? `${fmtSigned(doppler.tx_shift_hz, 0)} Hz` : '--'} />
            <DataCell label="TX Tune"  value={doppler ? `${fmtHz(doppler.tx_tune_hz)} Hz` : '--'} />
          </div>
        )}

        {!staticOn && (doppler && doppler.rx_hz !== doppler.tx_hz ? (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            <DataCell label="RX Signal Loss" value={`${doppler.rx_signal_loss_db.toFixed(1)} dB`} />
            <DataCell label="TX Signal Loss" value={`${doppler.tx_signal_loss_db.toFixed(1)} dB`} />
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            <DataCell label="Signal Loss" value={doppler ? `${doppler.rx_signal_loss_db.toFixed(1)} dB` : '--'} />
            <DataCell
              label="Signal Loss @ 100 MHz"
              value={doppler ? `${lossAtFreq(doppler.rx_signal_loss_db, doppler.rx_hz, GPREDICT_DEFAULT_HZ).toFixed(1)} dB` : '--'}
              tone={colors.textMuted}
            />
          </div>
        ))}

        {(error || actionError) && (
          <div
            className="rounded-md border px-2 py-1.5 text-[11px]"
            role="alert"
            style={{ color: colors.danger, borderColor: `${colors.danger}44`, backgroundColor: colors.dangerFill }}
          >
            {actionError ?? error}
          </div>
        )}
      </div>
    </section>
  )
}
