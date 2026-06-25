import { useEffect, useRef, useState, useCallback } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { colors } from '@/lib/colors'
import type { GssConfig, PlatformTrackingConfig } from '@/lib/types'
import { X, FileText, Database } from 'lucide-react'
import { authFetch } from '@/lib/auth'
import { GssInput } from '@/components/ui/gss-input'
import { parseTleBlock, joinTleBlock } from '@/lib/tle'

const springConfig = { type: 'spring' as const, stiffness: 500, damping: 30, mass: 0.8 }
const DEFAULT_DOPPLER_HZ = 437_575_000
let hasLoadedConfigSidebar = false

function diffConfig(current: GssConfig, base: GssConfig): Partial<GssConfig> | undefined
function diffConfig(current: unknown, base: unknown): unknown
function diffConfig(current: unknown, base: unknown): unknown {
  if (current === base) return undefined
  if (current === null || base === null || typeof current !== 'object' || typeof base !== 'object') {
    return current
  }
  if (Array.isArray(current) || Array.isArray(base)) {
    return JSON.stringify(current) === JSON.stringify(base) ? undefined : current
  }
  const cur = current as Record<string, unknown>
  const bas = base as Record<string, unknown>
  const out: Record<string, unknown> = {}
  for (const key of Object.keys(cur)) {
    const sub = diffConfig(cur[key], bas[key])
    if (sub !== undefined) out[key] = sub
  }
  return Object.keys(out).length === 0 ? undefined : out
}

/* -- helper sub-components ---------------------------------------- */

function InfoRow({ icon, label, value }: { icon?: React.ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2 text-xs">
      <span className="flex items-center gap-1.5 font-light shrink-0" style={{ color: colors.dim }}>
        {icon}
        {label}
      </span>
      <span className="font-mono text-right min-w-0 break-all" style={{ color: colors.value }}>{value}</span>
    </div>
  )
}

function Section({ title, children, show = true }: { title: string; children: React.ReactNode; show?: boolean }) {
  if (!show) return null
  return (
    <div className="mb-4">
      <div className="text-xs font-bold uppercase tracking-wider mb-2"
           style={{ color: colors.label }}>
        {title}
      </div>
      <div className="flex flex-col gap-2">{children}</div>
    </div>
  )
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex items-center justify-between gap-2">
      <span className="text-xs font-light shrink-0" style={{ color: colors.dim }}>{label}</span>
      <GssInput className="w-36 text-right" value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}

function NumberField({ label, value, onChange, compact }: { label: string; value: number; onChange: (v: number) => void; compact?: boolean }) {
  return (
    <label className="flex items-center justify-between gap-2">
      <span className="text-xs font-light shrink-0" style={{ color: colors.dim }}>{label}</span>
      <GssInput type="number" className={`${compact ? 'w-16' : 'w-36'} text-right`} value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </label>
  )
}

function ToggleField({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center justify-between gap-2 cursor-pointer">
      <span className="text-xs font-light" style={{ color: colors.dim }}>{label}</span>
      <button
        type="button"
        onClick={() => onChange(!value)}
        className="px-2 py-0.5 rounded text-xs font-medium color-transition"
        style={{
          backgroundColor: value ? `${colors.success}22` : 'transparent',
          color: value ? colors.success : colors.dim,
          border: `1px solid ${value ? `${colors.success}44` : colors.borderSubtle}`,
        }}
      >
        {value ? 'ON' : 'OFF'}
      </button>
    </label>
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function configLabel(key: string): string {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

type TrackingFrequencyKey = 'rx_hz' | 'tx_hz'
type RadioDirection = 'rx' | 'tx'

function parseFrequencyHz(value: string): number | null {
  const match = value.trim().toLowerCase().match(/^([0-9]+(?:\.[0-9]+)?)\s*(ghz|mhz|khz|hz)?$/)
  if (!match) return null
  const numeric = Number(match[1])
  if (!Number.isFinite(numeric)) return null
  const unit = match[2]
  const scale = unit === 'ghz'
    ? 1_000_000_000
    : unit === 'mhz' || (!unit && numeric < 10_000)
      ? 1_000_000
      : unit === 'khz'
        ? 1_000
        : 1
  return Math.round(numeric * scale)
}

function formatFrequencyLabel(hz: number): string {
  const mhz = hz / 1_000_000
  return `${Number(mhz.toFixed(6))} MHz`
}

function trackingFrequencyValue(config: GssConfig, key: TrackingFrequencyKey): number {
  const value = config.platform.tracking?.frequencies?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : DEFAULT_DOPPLER_HZ
}

function radioFrequencyValue(config: GssConfig, direction: RadioDirection): string {
  const configured = direction === 'rx' ? config.platform.rx.frequency : config.platform.tx.frequency
  if (typeof configured === 'string' && configured.trim()) return configured
  return formatFrequencyLabel(trackingFrequencyValue(config, direction === 'rx' ? 'rx_hz' : 'tx_hz'))
}

function MissionValueField({
  label,
  value,
  onChange,
}: {
  label: string
  value: unknown
  onChange: (value: unknown) => void
}) {
  if (typeof value === 'boolean') {
    return <ToggleField label={label} value={value} onChange={onChange} />
  }
  if (typeof value === 'number') {
    return <NumberField label={label} value={value} onChange={onChange} compact />
  }
  return (
    <TextField
      label={label}
      value={value === undefined || value === null ? '' : String(value)}
      onChange={onChange}
    />
  )
}

/* -- main component ----------------------------------------------- */

interface ConfigSidebarProps {
  open: boolean
  onClose: () => void
}

export function ConfigSidebar({ open, onClose }: ConfigSidebarProps) {
  const [cfg, setCfg] = useState<GssConfig | null>(null)
  const [tleDraft, setTleDraft] = useState('')
  const [dirty, setDirty] = useState(false)
  const [statusInfo, setStatusInfo] = useState<{ version: string; schema_path: string; schema_count: number; log_dir: string; session_log_json: string | null } | null>(null)
  const initialRef = useRef<GssConfig | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<Element | null>(null)
  const animateOnMount = hasLoadedConfigSidebar

  useEffect(() => {
    hasLoadedConfigSidebar = true
  }, [])

  // Capture trigger element when opening
  useEffect(() => {
    if (open) {
      triggerRef.current = document.activeElement
    }
  }, [open])

  // Focus trap: focus first input on mount, cycle Tab within panel, restore focus on close
  useEffect(() => {
    if (!open) {
      // Restore focus on close
      if (triggerRef.current && triggerRef.current instanceof HTMLElement) {
        triggerRef.current.focus()
      }
      return
    }

    const timer = setTimeout(() => {
      const panel = panelRef.current
      if (!panel) return
      const firstInput = panel.querySelector<HTMLElement>('input, button, [tabindex]')
      firstInput?.focus()
    }, 100)

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        handleCancel()
        return
      }
      if (e.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const focusable = panel.querySelectorAll<HTMLElement>(
        'input, button, [tabindex]:not([tabindex="-1"])'
      )
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault()
          last.focus()
        }
      } else {
        if (document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('keydown', handleKeyDown)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // Fetch config + status on open
  useEffect(() => {
    if (!open) return
    fetch('/api/config')
      .then((r) => r.json())
      .then((data: GssConfig) => {
        setCfg(data)
        initialRef.current = JSON.parse(JSON.stringify(data))
        setTleDraft(joinTleBlock(data.platform.tracking?.tle ?? {}))
        setDirty(false)
      })
      .catch(() => {})
    fetch('/api/status')
      .then((r) => r.json())
      .then(setStatusInfo)
      .catch(() => {})
  }, [open])

  const handleSave = useCallback(async () => {
    if (!cfg || !initialRef.current) return false
    const update = diffConfig(cfg, initialRef.current) ?? {}
    if (Object.keys(update).length === 0) {
      setDirty(false)
      return true
    }
    try {
      const response = await authFetch('/api/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(update),
      })
      if (!response.ok) return false
      initialRef.current = JSON.parse(JSON.stringify(cfg))
      setDirty(false)
      return true
    } catch {
      return false
    }
  }, [cfg])

  const handleCancel = useCallback(() => {
    if (initialRef.current) {
      setCfg(JSON.parse(JSON.stringify(initialRef.current)))
      setTleDraft(joinTleBlock(initialRef.current.platform.tracking?.tle ?? {}))
    }
    setDirty(false)
    onClose()
  }, [onClose])

  const updatePlatform = useCallback(<K extends keyof GssConfig['platform']>(
    section: K,
    key: string,
    value: unknown,
  ) => {
    setCfg((prev) => {
      if (!prev) return prev
      const next = {
        ...prev,
        platform: { ...prev.platform, [section]: { ...prev.platform[section], [key]: value } },
      }
      setDirty(true)
      return next
    })
  }, [])

  const updateRadioFrequency = useCallback((direction: RadioDirection, value: string) => {
    setCfg((prev) => {
      if (!prev) return prev
      const hz = parseFrequencyHz(value)
      const tracking = prev.platform.tracking
      const frequencies = tracking?.frequencies
      const nextPlatform = { ...prev.platform }
      if (direction === 'rx') {
        nextPlatform.rx = { ...prev.platform.rx, frequency: value }
      } else {
        nextPlatform.tx = { ...prev.platform.tx, frequency: value }
      }
      if (hz !== null) {
        nextPlatform.tracking = {
          ...(tracking ?? {}),
          frequencies: {
            ...(frequencies ?? {}),
            [direction === 'rx' ? 'rx_hz' : 'tx_hz']: hz,
          },
        } as PlatformTrackingConfig
      }
      const next = { ...prev, platform: nextPlatform }
      setDirty(true)
      return next
    })
  }, [])

  const updateTrackingTle = useCallback((patch: Partial<PlatformTrackingConfig['tle']>) => {
    setCfg((prev) => {
      if (!prev) return prev
      const tracking = prev.platform.tracking
      const next = {
        ...prev,
        platform: {
          ...prev.platform,
          tracking: {
            ...(tracking ?? {}),
            tle: { ...(tracking?.tle ?? {}), ...patch },
          } as PlatformTrackingConfig,
        },
      }
      setDirty(true)
      return next
    })
  }, [])

  const updateTrackingFetch = useCallback((patch: Partial<NonNullable<PlatformTrackingConfig['tle_fetch']>>) => {
    setCfg((prev) => {
      if (!prev) return prev
      const tracking = prev.platform.tracking
      const next = {
        ...prev,
        platform: {
          ...prev.platform,
          tracking: {
            ...(tracking ?? {}),
            tle_fetch: { identifier: '', auto_refresh: false, refresh_interval_hours: 12,
                         ...(tracking?.tle_fetch ?? {}), ...patch },
          } as PlatformTrackingConfig,
        },
      }
      setDirty(true)
      return next
    })
  }, [])

  const [fetchMsg, setFetchMsg] = useState<string>('')
  const [fetching, setFetching] = useState(false)

  const handleFetchTle = useCallback(async () => {
    setFetching(true)
    setFetchMsg('')
    try {
      const resp = await authFetch('/api/tracking/tle/fetch', { method: 'POST' })
      const data = await resp.json()
      if (data.ok) {
        const tle = { source: `${data.via} (fetched)`, name: data.name,
                      line1: data.line1, line2: data.line2, method: 'fetched' as const,
                      fetched_at_ms: data.tle_epoch_ms }
        updateTrackingTle(tle)
        setTleDraft(joinTleBlock(tle))
        const age = data.age_days != null ? ` · epoch ${data.age_days}d old` : ''
        setFetchMsg(`Fetched via ${data.via}${age}. Review and Save.`)
      } else {
        setFetchMsg(data.detail || 'Fetch failed')
      }
    } catch {
      setFetchMsg('Fetch request failed')
    } finally {
      setFetching(false)
    }
  }, [updateTrackingTle])

  // The textarea holds the raw paste; only the lines that successfully
  // parse are written back, so a partial edit never wipes a good field.
  const handleTleDraftChange = useCallback((text: string) => {
    setTleDraft(text)
    updateTrackingTle({ ...parseTleBlock(text), method: 'manual' })
  }, [updateTrackingTle])

  const updateMission = useCallback((
    section: string,
    key: string,
    value: unknown,
  ) => {
    setCfg((prev) => {
      if (!prev) return prev
      const next = {
        ...prev,
        mission: {
          ...prev.mission,
          config: {
            ...prev.mission.config,
            [section]: { ...(isRecord(prev.mission.config[section]) ? prev.mission.config[section] : {}), [key]: value },
          },
        },
      }
      setDirty(true)
      return next
    })
  }, [])

  const updateMissionTopLevel = useCallback((key: string, value: unknown) => {
    setCfg((prev) => {
      if (!prev) return prev
      const next = {
        ...prev,
        mission: {
          ...prev.mission,
          config: {
            ...prev.mission.config,
            [key]: value,
          },
        },
      }
      setDirty(true)
      return next
    })
  }, [])

  return (
    <AnimatePresence initial={false}>
      {open && (
        <div className="fixed inset-0 z-50 flex">
          {/* Backdrop */}
          <motion.div
            className="flex-1"
            style={{ backgroundColor: colors.modalBackdrop }}
            initial={animateOnMount ? { opacity: 0 } : false}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={handleCancel}
          />

          {/* Panel */}
          <motion.div
            ref={panelRef}
            className="w-96 h-full overflow-y-auto p-4 border-l shadow-overlay"
            style={{ backgroundColor: colors.bgPanelRaised, borderColor: colors.borderStrong }}
            initial={animateOnMount ? { x: 384 } : false}
            animate={{ x: 0 }}
            exit={{ x: 384 }}
            transition={springConfig}
          >
            {/* Header */}
            <div className="flex items-center justify-between mb-4">
              <span className="text-sm font-bold" style={{ color: colors.label }}>Configuration</span>
              <button onClick={handleCancel} className="p-1 rounded hover:bg-white/5">
                <X className="size-4" style={{ color: colors.dim }} />
              </button>
            </div>

            {!cfg ? (
              <div className="text-xs" style={{ color: colors.dim }}>Loading...</div>
            ) : (
              <>
                {Object.entries(cfg.mission.config).some(([, value]) => !isRecord(value)) && (
                  <Section title={cfg.mission.name || cfg.mission.id || 'Mission'}>
                    {Object.entries(cfg.mission.config)
                      .filter(([, value]) => !isRecord(value))
                      .map(([key, value]) => (
                        <MissionValueField
                          key={key}
                          label={configLabel(key)}
                          value={value}
                          onChange={(v) => updateMissionTopLevel(key, v)}
                        />
                      ))}
                  </Section>
                )}
                {Object.entries(cfg.mission.config)
                  .filter(([, value]) => isRecord(value))
                  .map(([section, value]) => (
                    <Section key={section} title={configLabel(section)}>
                      {Object.entries(value as Record<string, unknown>).map(([key, nestedValue]) => (
                        <MissionValueField
                          key={key}
                          label={configLabel(key)}
                          value={nestedValue}
                          onChange={(v) => updateMission(section, key, v)}
                        />
                      ))}
                    </Section>
                  ))}

                {/* System */}
                <Section title="System">
                  <TextField label="RX Frequency" value={radioFrequencyValue(cfg, 'rx')} onChange={(v) => updateRadioFrequency('rx', v)} />
                  <TextField label="TX Frequency" value={radioFrequencyValue(cfg, 'tx')} onChange={(v) => updateRadioFrequency('tx', v)} />
                  <TextField label="RX ZMQ Address" value={cfg.platform.rx.zmq_addr} onChange={(v) => updatePlatform('rx', 'zmq_addr', v)} />
                  <TextField label="TX ZMQ Address" value={cfg.platform.tx.zmq_addr} onChange={(v) => updatePlatform('tx', 'zmq_addr', v)} />
                  <NumberField label="TX Delay (ms)" value={cfg.platform.tx.delay_ms} onChange={(v) => updatePlatform('tx', 'delay_ms', v)} />
                  <NumberField label="TX→RX Blackout (ms)" value={cfg.platform.rx.tx_blackout_ms ?? 0} onChange={(v) => updatePlatform('rx', 'tx_blackout_ms', v)} />
                </Section>

                {/* Tracking / Doppler — TLE applies live on the next 1 Hz tick, no restart */}
                <Section title="Tracking">
                  <TextField
                    label="TLE Source"
                    value={cfg.platform.tracking?.tle?.source ?? ''}
                    onChange={(v) => updateTrackingTle({ source: v })}
                  />
                  <label className="flex flex-col gap-1">
                    <span className="text-xs font-light" style={{ color: colors.dim }}>Two-Line Elements</span>
                    <textarea
                      value={tleDraft}
                      onChange={(e) => handleTleDraftChange(e.target.value)}
                      rows={3}
                      spellCheck={false}
                      placeholder={'SATNAME\n1 NNNNNU ...\n2 NNNNN ...'}
                      className="w-full font-mono text-xs rounded px-2 py-1 resize-y leading-snug"
                      style={{ backgroundColor: colors.bgApp, color: colors.value, border: `1px solid ${colors.borderSubtle}` }}
                    />
                  </label>
                  <TextField
                    label="Fetch Identifier"
                    value={cfg.platform.tracking?.tle_fetch?.identifier ?? ''}
                    onChange={(v) => updateTrackingFetch({ identifier: v })}
                  />
                  <button
                    type="button"
                    disabled={fetching || !(cfg.platform.tracking?.tle_fetch?.identifier ?? '').trim()}
                    onClick={() => { void handleFetchTle() }}
                    className="text-xs rounded px-2 py-1 self-start disabled:opacity-40"
                    style={{ border: `1px solid ${colors.borderSubtle}`, color: colors.value }}
                  >
                    {fetching ? 'Fetching…' : 'Fetch TLE'}
                  </button>
                  {fetchMsg && (
                    <span className="text-xs" style={{ color: colors.dim }}>{fetchMsg}</span>
                  )}
                  <ToggleField
                    label="Auto-refresh"
                    value={cfg.platform.tracking?.tle_fetch?.auto_refresh ?? false}
                    onChange={(v) => updateTrackingFetch({ auto_refresh: v })}
                  />
                  <NumberField
                    label="Refresh (hours)"
                    value={cfg.platform.tracking?.tle_fetch?.refresh_interval_hours ?? 12}
                    onChange={(v) => updateTrackingFetch({ refresh_interval_hours: v })}
                  />
                </Section>

                {/* Session Info */}
                {statusInfo && (
                  <Section title="Session">
                    <InfoRow icon={<Database className="size-3" />} label="Version" value={statusInfo.version} />
                    <InfoRow icon={<FileText className="size-3" />} label="Schema" value={(statusInfo.schema_path || '').split('/').pop() ?? ''} />
                    <InfoRow label="Commands" value={String(statusInfo.schema_count)} />
                    <InfoRow label="Log Dir" value={statusInfo.log_dir} />
                    {statusInfo.session_log_json && <InfoRow label="Session Data" value={statusInfo.session_log_json.split('/').pop() ?? ''} />}
                  </Section>
                )}

                {/* Save / Cancel */}
                <div className="flex items-center gap-2 mt-4 pt-3 border-t" style={{ borderColor: colors.borderSubtle }}>
                  <button
                    onClick={handleCancel}
                    className="flex-1 px-3 py-1.5 rounded text-xs border"
                    style={{ color: colors.dim, borderColor: colors.borderSubtle }}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={() => { void handleSave().then((saved) => { if (saved) onClose() }) }}
                    disabled={!dirty}
                    className="flex-1 px-3 py-1.5 rounded text-xs font-bold disabled:opacity-30 btn-feedback"
                    style={{ backgroundColor: dirty ? colors.success : colors.borderSubtle, color: colors.bgApp }}
                  >
                    Save & Close
                  </button>
                </div>
              </>
            )}
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
