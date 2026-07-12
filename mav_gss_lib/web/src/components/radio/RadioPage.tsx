import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  Monitor,
  Play,
  Radio as RadioWave,
  RotateCcw,
  Square,
  Terminal,
  X,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { StatusDot } from '@/components/shared/atoms/StatusDot'
import { useConfig } from '@/state/sessionHooks'
import { colors } from '@/lib/colors'
import { cn } from '@/lib/utils'
import { lineColor } from './lineColor'
import { type RadioStatus } from './useRadioSocket'
import { useRadio } from '@/state/radioHooks'
import { useTracking } from '@/state/trackingHooks'
import { DopplerSection } from './DopplerSection'

function fmtUptime(startedAtMs: number | null, fallbackSeconds: number): string {
  const total = startedAtMs ? Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000)) : Math.floor(fallbackSeconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function fmtSeconds(seconds: number): string {
  const total = Math.floor(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function basename(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? path
}

function processDot(status: RadioStatus): { status: string; label: string } {
  switch (status.state) {
    case 'running':  return { status: 'LIVE',     label: 'RUNNING' }
    case 'stopping': return { status: 'STOPPING', label: 'STOPPING' }
    case 'crashed':  return { status: 'CRASHED',  label: 'CRASHED' }
    default:         return { status: 'STOPPED',  label: 'STOPPED' }
  }
}

function btnTone(enabled: boolean, accent: string) {
  return enabled
    ? { color: accent, borderColor: `${accent}66`, backgroundColor: `${accent}08` }
    : { color: colors.textDisabled, borderColor: colors.borderSubtle, backgroundColor: 'transparent' }
}

function PanelHeader({ icon, title, right }: { icon: ReactNode; title: string; right?: ReactNode }) {
  return (
    <div className="flex min-h-[33px] shrink-0 items-center justify-between gap-3 border-b px-3 py-1.5" style={{ borderColor: colors.borderSubtle }}>
      <div className="flex min-w-0 items-center gap-2">
        {icon}
        <span className="truncate text-xs font-bold uppercase tracking-wide" style={{ color: colors.value }}>{title}</span>
      </div>
      {right}
    </div>
  )
}

function DataCell({
  label,
  value,
  tone,
  className,
  wrap = false,
  titleOverride,
}: {
  label: string
  value: string
  tone?: string
  className?: string
  wrap?: boolean
  titleOverride?: string
}) {
  return (
    <div className={cn('min-w-0 py-1', className)}>
      <div className="text-[11px] font-medium uppercase" style={{ color: colors.textMuted }}>{label}</div>
      <div
        title={titleOverride ?? value}
        className={cn('mt-0.5 min-w-0 font-mono text-xs', wrap ? 'break-all' : 'truncate')}
        style={{ color: tone ?? colors.textPrimary }}
      >
        {value}
      </div>
    </div>
  )
}

export function RadioPage() {
  const { config } = useConfig()
  const { status, logs, connected, lastUpdateMs, busy, actionError, runAction, dismissError } = useRadio()
  const tracking = useTracking()
  const [apiStatus, setApiStatus] = useState<{ zmq_rx?: string; zmq_tx?: string }>({})
  const [, setNowTick] = useState(0)
  const logRef = useRef<HTMLDivElement>(null)
  const stickyRef = useRef(true)

  useEffect(() => {
    let cancelled = false
    const refreshApiStatus = async () => {
      try {
        const r = await fetch('/api/status', { cache: 'no-store' })
        if (!r.ok) return
        const data = await r.json()
        if (cancelled) return
        setApiStatus(data as { zmq_rx?: string; zmq_tx?: string })
      } catch { /* ignore */ }
    }
    refreshApiStatus()
    const id = window.setInterval(refreshApiStatus, 3000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [])

  useEffect(() => {
    const id = window.setInterval(() => setNowTick(n => n + 1), 1000)
    return () => window.clearInterval(id)
  }, [])

  const connTone = connected ? colors.success : colors.danger
  const connLabel = connected ? 'CONNECTED' : 'DISCONNECTED'

  const rxStatus = status.state === 'stopped' ? 'WAITING' : (apiStatus.zmq_rx || 'DOWN')

  const startEnabled = status.enabled && !status.running && busy === null
  const stopEnabled = status.running && busy === null
  const restartEnabled = status.enabled && status.running && busy === null

  const onLogScroll = () => {
    const node = logRef.current
    if (!node) return
    stickyRef.current = node.scrollHeight - node.clientHeight - node.scrollTop < 32
  }

  useEffect(() => {
    const node = logRef.current
    if (node && stickyRef.current) node.scrollTop = node.scrollHeight
  }, [logs])

  const dot = processDot(status)

  void lastUpdateMs

  return (
    <div className="flex h-full min-h-0 flex-col p-4">
      {actionError && (
        <div
          className="mb-2 flex items-center justify-between gap-3 rounded-md border px-3 py-1.5 text-[11px]"
          style={{ color: colors.danger, borderColor: `${colors.danger}66`, backgroundColor: colors.dangerFill }}
          role="alert"
        >
          <span className="truncate">Action failed: {actionError}</span>
          <button onClick={dismissError} className="shrink-0 opacity-80 hover:opacity-100" aria-label="Dismiss error">
            <X className="size-3.5" />
          </button>
        </div>
      )}
      <div
        className={cn(
          'flex-1 min-h-0 gap-3',
          'grid grid-cols-[minmax(320px,0.62fr)_minmax(520px,1.38fr)]',
          '2xl:grid-cols-[minmax(360px,0.45fr)_minmax(720px,1.55fr)]',
          'max-[980px]:grid-cols-none max-[980px]:flex max-[980px]:flex-col max-[980px]:overflow-y-auto',
        )}
      >
        <div className="flex min-h-0 flex-col gap-3 max-[980px]:order-2">
          <section className="flex flex-col rounded-lg border shadow-panel" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
            <PanelHeader
              icon={<Terminal className="size-3.5 shrink-0" style={{ color: colors.dim }} />}
              title="GNU Radio Process"
              right={(
                <div className="flex items-center gap-2">
                  <Badge
                    variant="outline"
                    className="h-5 rounded text-[11px]"
                    style={{ color: connTone, borderColor: `${connTone}66`, backgroundColor: 'transparent' }}
                  >
                    {connLabel}
                  </Badge>
                  <StatusDot status={dot.status} label={dot.label} />
                </div>
              )}
            />
            <div className="flex flex-col gap-1.5 px-3 py-2">
              <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                <DataCell label="Script" value={basename(status.script) || '--'} titleOverride={status.script || ''} />
                <DataCell label="Stdout Log" value={status.log_file ? basename(status.log_file) : '--'} titleOverride={status.log_file || ''} />
              </div>
              <div className="grid grid-cols-3 gap-x-4 gap-y-1">
                <DataCell label="PID" value={status.pid === null ? '--' : String(status.pid)} />
                <DataCell
                  label={status.running ? 'Uptime' : 'Last Runtime'}
                  value={
                    status.running
                      ? fmtUptime(status.started_at_ms, status.uptime_s)
                      : (status.last_runtime_s > 0 ? fmtSeconds(status.last_runtime_s) : '00:00:00')
                  }
                  tone={status.running ? colors.success : colors.textMuted}
                />
                <DataCell label="Exit Code" value={status.exit_code === null ? '--' : String(status.exit_code)} tone={status.state === 'crashed' ? colors.danger : undefined} />
              </div>
              {status.running && status.stream_health && (
                <div className="grid grid-cols-3 gap-x-4 gap-y-1">
                  <DataCell
                    label="RF Level"
                    value={`${status.stream_health.rms_dbfs.toFixed(1)} dBFS`}
                    titleOverride={`pre-FIR rms over the last ${status.stream_health.span_s}s; peak ${status.stream_health.peak_dbfs.toFixed(1)} dBFS`}
                    tone={status.stream_health.peak_dbfs > -6 ? colors.warning : undefined}
                  />
                  <DataCell
                    label="Clip"
                    value={String(status.stream_health.clip_count)}
                    titleOverride="near-full-scale samples in the last report window"
                    tone={status.stream_health.clip_count > 0 ? colors.danger : colors.textMuted}
                  />
                  <DataCell
                    label="Overflows"
                    value={String(status.stream_health.overflows_total)}
                    titleOverride="UHD stream discontinuities since radio start (dropped samples)"
                    tone={status.stream_health.overflows_total > 0 ? colors.danger : colors.textMuted}
                  />
                </div>
              )}
              {status.error && (
                <div
                  className="rounded-md border px-2 py-1.5 text-[11px]"
                  role="alert"
                  style={{ color: colors.danger, borderColor: `${colors.danger}44`, backgroundColor: colors.dangerFill }}
                >
                  {status.error}
                </div>
              )}
              <div className={cn('mt-1 grid gap-2', status.running ? 'grid-cols-3' : 'grid-cols-2')}>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!startEnabled}
                  aria-busy={busy === 'start'}
                  aria-disabled={!startEnabled}
                  onClick={() => void runAction('start')}
                  className="h-8 gap-1.5 text-xs font-bold btn-feedback"
                  style={btnTone(startEnabled, colors.active)}
                >
                  <Play data-icon="inline-start" />
                  {busy === 'start' ? 'Starting…' : 'Start'}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!stopEnabled}
                  aria-busy={busy === 'stop'}
                  aria-disabled={!stopEnabled}
                  onClick={() => void runAction('stop')}
                  className="h-8 gap-1.5 text-xs font-bold btn-feedback"
                  style={btnTone(stopEnabled, colors.danger)}
                >
                  <Square data-icon="inline-start" />
                  {busy === 'stop' ? 'Stopping…' : 'Stop'}
                </Button>
                {status.running && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!restartEnabled}
                    aria-busy={busy === 'restart'}
                    aria-disabled={!restartEnabled}
                    onClick={() => void runAction('restart')}
                    className="h-8 gap-1.5 text-xs font-bold btn-feedback"
                    style={btnTone(restartEnabled, colors.info)}
                  >
                    <RotateCcw data-icon="inline-start" />
                    {busy === 'restart' ? 'Restarting…' : 'Restart'}
                  </Button>
                )}
              </div>
            </div>
          </section>

          <DopplerSection
            doppler={tracking.doppler}
            mode={tracking.mode}
            error={tracking.error}
            busy={tracking.busy}
            actionError={tracking.actionError}
            engage={tracking.engage}
            disengage={tracking.disengage}
            dismissError={tracking.dismissError}
          />

          <section className="flex flex-col rounded-lg border shadow-panel" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
            <PanelHeader
              icon={<RadioWave className="size-3.5 shrink-0" style={{ color: colors.dim }} />}
              title="ZMQ Links"
            />
            <div className="flex flex-col gap-2 px-3 py-2">
              <LinkRow label="RX" value={config?.platform.rx.zmq_addr ?? '--'} status={rxStatus} />
              <LinkRow label="TX" value={config?.platform.tx.zmq_addr ?? '--'} status={apiStatus.zmq_tx || 'DOWN'} />
            </div>
          </section>

          <section className="flex min-h-0 flex-1 flex-col rounded-lg border shadow-panel" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
            <PanelHeader
              icon={<Monitor className="size-3.5 shrink-0" style={{ color: colors.dim }} />}
              title="Qt Flowgraph Window"
              right={(
                <Badge variant="outline" className="h-5 rounded text-[11px]" style={{ color: status.running ? colors.success : colors.textMuted, borderColor: colors.borderSubtle }}>
                  {status.running ? 'visible separately' : 'closed'}
                </Badge>
              )}
            />
            <div className="flex flex-1 flex-col gap-1.5 px-3 py-2">
              <DataCell label="Autostart" value={status.autostart ? 'enabled' : 'disabled'} tone={status.autostart ? colors.warning : colors.textMuted} />
              <DataCell label="CWD" value={status.cwd || '--'} wrap />
            </div>
          </section>
        </div>

        <section className="flex min-h-0 flex-col rounded-lg border shadow-panel max-[980px]:order-1 max-[980px]:min-h-[40vh]" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
          <PanelHeader
            icon={<Terminal className="size-3.5 shrink-0" style={{ color: colors.dim }} />}
            title="GNU Radio Terminal"
            right={(
              <Badge variant="outline" className="h-5 rounded text-[11px]" style={{ color: connected ? colors.textSecondary : colors.textMuted, borderColor: colors.borderSubtle, backgroundColor: 'transparent' }}>
                stdout + stderr
              </Badge>
            )}
          />
          <div className="flex min-h-[32px] items-center gap-2 border-b px-3 py-1.5" style={{ borderColor: colors.borderSubtle }}>
            <span className="min-w-0 flex-1 truncate font-mono text-[11px]" style={{ color: colors.textMuted }}>
              {status.command.join(' ') || status.script || 'No radio process has been started'}
            </span>
          </div>
          <div ref={logRef} onScroll={onLogScroll} className="flex-1 min-h-0 overflow-auto p-2 font-mono text-[11px] leading-relaxed" style={{ backgroundColor: '#070707' }}>
            {logs.length === 0 ? (
              <div className="flex h-full items-center justify-center" style={{ color: colors.textMuted }}>
                No GNU Radio output yet
              </div>
            ) : logs.map((entry) => (
              <div key={entry.id} className="whitespace-pre-wrap break-words" style={{ color: lineColor(entry.text), minHeight: entry.text === '' ? '1.4em' : undefined }}>
                {entry.text || ' '}
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}

function LinkRow({ label, value, status }: { label: string; value: string; status: string }) {
  return (
    <div className="min-w-0 py-1 text-[11px]">
      <div className="flex items-center gap-2">
        <span className="font-bold uppercase" style={{ color: colors.textMuted }}>{label}</span>
        <StatusDot status={status} />
      </div>
      <div title={value} className="mt-1 truncate font-mono text-xs" style={{ color: colors.textPrimary }}>
        {value}
      </div>
    </div>
  )
}
