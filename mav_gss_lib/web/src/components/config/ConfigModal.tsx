import { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { colors } from '@/lib/colors'
import type { GssConfig, MissionInfo, PlatformTrackingConfig } from '@/lib/types'
import { X, Settings, Rocket, Radio, Satellite, Info } from 'lucide-react'
import { authFetch } from '@/lib/auth'
import { parseTleBlock, joinTleBlock } from '@/lib/tle'
import { waitForMissionThenReload } from '@/lib/restart'
import { Kbd } from '@/components/ui/kbd'
import { ConfirmDialog } from '@/components/shared/dialogs/ConfirmDialog'
import { ConfigRail, type RailItem } from './ConfigRail'
import { SearchContext, matchesQuery } from './search'
import { PaneRenderer, type SettingRow, type SettingPane } from './fields'

const springConfig = { type: 'spring' as const, stiffness: 500, damping: 30, mass: 0.8 }
const DEFAULT_DOPPLER_HZ = 437_575_000
let hasLoadedConfigModal = false

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

const RAIL_ITEMS: RailItem[] = [
  { id: 'mission', label: 'Mission', group: 'Mission', Icon: Rocket },
  { id: 'radio', label: 'Radio / RF', group: 'Platform', Icon: Radio },
  { id: 'tracking', label: 'Tracking', group: 'Platform', Icon: Satellite },
  { id: 'about', label: 'About', group: 'System', Icon: Info },
]

interface TargetBird {
  id: string
  label: string
  norad: number
  rx_frequency: string
}

function targetBirds(config: GssConfig): TargetBird[] {
  const raw = config.mission.config.target_birds
  if (!Array.isArray(raw)) return []
  return raw.filter((b): b is TargetBird =>
    isRecord(b) && typeof b.id === 'string' && typeof b.label === 'string'
    && typeof b.norad === 'number' && typeof b.rx_frequency === 'string')
}

function missionRow(key: string, value: unknown, onChange: (v: unknown) => void): SettingRow {
  const label = configLabel(key)
  if (typeof value === 'boolean') return { id: key, label, control: { kind: 'toggle', value, onChange: (v) => onChange(v) } }
  if (typeof value === 'number') return { id: key, label, control: { kind: 'number', value, onChange: (v) => onChange(v) } }
  return { id: key, label, control: { kind: 'text', value: value == null ? '' : String(value), onChange: (v) => onChange(v) } }
}

/* -- main component ----------------------------------------------- */

interface ConfigModalProps {
  open: boolean
  onClose: () => void
}

export function ConfigModal({ open, onClose }: ConfigModalProps) {
  const [cfg, setCfg] = useState<GssConfig | null>(null)
  const [tleDraft, setTleDraft] = useState('')
  const [dirty, setDirty] = useState(false)
  const [statusInfo, setStatusInfo] = useState<{ version: string; schema_path: string; schema_count: number; log_dir: string; session_log_json: string | null } | null>(null)
  const initialRef = useRef<GssConfig | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<Element | null>(null)
  const animateOnMount = hasLoadedConfigModal
  const [activeCategory, setActiveCategory] = useState('radio')
  const [search, setSearch] = useState('')
  const [credStatus, setCredStatus] = useState<{ identity_set: boolean; password_set: boolean } | null>(null)
  const [missions, setMissions] = useState<MissionInfo[]>([])
  const [pendingSwitch, setPendingSwitch] = useState<MissionInfo | null>(null)
  const [switchingTo, setSwitchingTo] = useState<MissionInfo | null>(null)
  const [switchError, setSwitchError] = useState<string | null>(null)

  useEffect(() => {
    hasLoadedConfigModal = true
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
    fetch('/api/tracking/tle/status')
      .then((r) => r.json())
      .then((d) => setCredStatus(d.spacetrack ?? null))
      .catch(() => {})
    fetch('/api/missions')
      .then((r) => r.json())
      .then((d) => setMissions(Array.isArray(d.missions) ? d.missions : []))
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
    setPendingSwitch(null)
    onClose()
  }, [onClose])

  const handleConfirmSwitch = useCallback(async () => {
    if (!pendingSwitch) return
    const target = pendingSwitch
    setPendingSwitch(null)
    setSwitchError(null)
    try {
      const response = await authFetch('/api/mission/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: target.id }),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        setSwitchError(String(data.error ?? `switch failed (${response.status})`))
        return
      }
      setSwitchingTo(target)
      void waitForMissionThenReload(target.id)
    } catch {
      setSwitchError('switch request failed')
    }
  }, [pendingSwitch])

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

  const updateTrackingControl = useCallback((patch: Partial<NonNullable<PlatformTrackingConfig['control']>>) => {
    setCfg((prev) => {
      if (!prev) return prev
      const tracking = prev.platform.tracking
      const next = {
        ...prev,
        platform: {
          ...prev.platform,
          tracking: {
            ...(tracking ?? {}),
            control: { ...(tracking?.control ?? {}), ...patch },
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
      // Send the draft identifier so Fetch works on the unsaved edit (e.g. a
      // fresh Target-satellite selection) instead of the last-saved value.
      const identifier = (cfg?.platform.tracking?.tle_fetch?.identifier ?? '').trim()
      const resp = await authFetch('/api/tracking/tle/fetch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(identifier ? { identifier } : {}),
      })
      const data = await resp.json()
      if (data.ok) {
        const tle = { source: `${data.via} (fetched)`, name: data.name,
                      line1: data.line1, line2: data.line2, method: 'fetched' as const,
                      fetched_at_ms: Date.now() }
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
  }, [cfg, updateTrackingTle])

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

  const panes = useMemo<SettingPane[]>(() => {
    if (!cfg) return []
    const out: SettingPane[] = []

    const missionGroups: SettingPane['groups'] = []
    if (missions.length > 0) {
      missionGroups.push({
        title: 'Active Mission',
        rows: [{
          id: 'active_mission',
          label: 'Mission',
          description: switchError
            ? `Switch failed: ${switchError}`
            : 'Switching stops the radio process and restarts the server.',
          control: {
            kind: 'select',
            value: cfg.mission.id,
            options: missions.map((m) => ({ value: m.id, label: m.name })),
            onChange: (v) => {
              if (v === cfg.mission.id) return
              const target = missions.find((m) => m.id === v)
              if (target) setPendingSwitch(target)
            },
          },
        }],
      })
    }
    const birds = targetBirds(cfg)
    if (birds.length) {
      const identifier = (cfg.platform.tracking?.tle_fetch?.identifier ?? '').trim()
      const selected = birds.find((b) => String(b.norad) === identifier)
      const savedRxHz = initialRef.current ? parseFrequencyHz(radioFrequencyValue(initialRef.current, 'rx')) : null
      const description = !selected
        ? 'Fills the TLE identifier and RX frequency; Fetch the TLE, then Save.'
        : parseFrequencyHz(selected.rx_frequency) === savedRxHz
          ? 'Same frequency as saved — Fetch the TLE, Save, then re-engage Doppler.'
          : 'Frequency differs from saved — Fetch the TLE, Save, then restart the radio.'
      missionGroups.push({
        title: 'Target Satellite',
        rows: [{
          id: 'target_bird',
          label: 'Target satellite',
          description,
          control: {
            kind: 'select',
            value: selected?.id ?? '',
            options: [
              ...(selected ? [] : [{ value: '', label: '—' }]),
              ...birds.map((b) => ({ value: b.id, label: `${b.label} · ${b.rx_frequency}` })),
            ],
            onChange: (v) => {
              const bird = birds.find((b) => b.id === v)
              if (!bird) return
              updateTrackingFetch({ identifier: String(bird.norad) })
              updateRadioFrequency('rx', bird.rx_frequency)
            },
          },
        }],
      })
    }
    const scalars = Object.entries(cfg.mission.config)
      .filter(([key, v]) => !isRecord(v) && key !== 'target_birds')
    if (scalars.length) {
      missionGroups.push({
        title: cfg.mission.name || cfg.mission.id || 'Mission',
        rows: scalars.map(([key, value]) => missionRow(key, value, (v) => updateMissionTopLevel(key, v))),
      })
    }
    for (const [section, value] of Object.entries(cfg.mission.config).filter(([, v]) => isRecord(v))) {
      missionGroups.push({
        title: configLabel(section),
        rows: Object.entries(value as Record<string, unknown>).map(([key, nested]) =>
          missionRow(key, nested, (v) => updateMission(section, key, v))),
      })
    }
    out.push({ id: 'mission', title: cfg.mission.name || 'Mission', description: 'Mission-specific parameters.', groups: missionGroups })

    out.push({
      id: 'radio', title: 'Radio / RF', description: 'Frequencies, ZMQ transport, and uplink timing.',
      groups: [
        { title: 'Frequencies', rows: [
          { id: 'rx_freq', label: 'RX frequency', description: 'Downlink center frequency.', control: { kind: 'text', value: radioFrequencyValue(cfg, 'rx'), onChange: (v) => updateRadioFrequency('rx', v) } },
          { id: 'tx_freq', label: 'TX frequency', description: 'Uplink center frequency.', control: { kind: 'text', value: radioFrequencyValue(cfg, 'tx'), onChange: (v) => updateRadioFrequency('tx', v) } },
        ]},
        { title: 'Transport (ZMQ)', rows: [
          { id: 'rx_zmq', label: 'RX ZMQ address', description: 'Inbound PDU socket.', control: { kind: 'text', stacked: true, value: cfg.platform.rx.zmq_addr, onChange: (v) => updatePlatform('rx', 'zmq_addr', v) } },
          { id: 'tx_zmq', label: 'TX ZMQ address', description: 'Outbound publish socket.', control: { kind: 'text', stacked: true, value: cfg.platform.tx.zmq_addr, onChange: (v) => updatePlatform('tx', 'zmq_addr', v) } },
        ]},
        { title: 'Timing', rows: [
          { id: 'tx_delay', label: 'TX delay', description: 'Pause before each uplink frame.', control: { kind: 'number', unit: 'ms', value: cfg.platform.tx.delay_ms, onChange: (v) => updatePlatform('tx', 'delay_ms', v) } },
          { id: 'blackout', label: 'TX → RX blackout', description: 'RX mute window after TX (half-duplex).', control: { kind: 'number', unit: 'ms', value: cfg.platform.rx.tx_blackout_ms ?? 0, onChange: (v) => updatePlatform('rx', 'tx_blackout_ms', v) } },
        ]},
        { title: 'Command handling', rows: [
          { id: 'verifiers_enabled', label: 'Command verifiers', description: 'Experimental satellite-hunting only. Keep enabled for normal operations; disabling cancels active verification and permits repeated commands without response-window gating.', control: { kind: 'toggle', value: cfg.platform.tx.verifiers_enabled ?? true, onChange: (v) => updatePlatform('tx', 'verifiers_enabled', v) } },
        ]},
        { title: 'Front end', rows: [
          { id: 'rx_gain', label: 'RX gain', description: 'Flowgraph boot gain (B210, max 76 dB) — the GUI slider still overrides live. Stamped into capture metadata. Applies on radio start.', control: { kind: 'number', unit: 'dB', value: cfg.platform.radio?.rx_gain ?? 40, onChange: (v) => updatePlatform('radio', 'rx_gain', v) } },
          { id: 'rx_bw', label: 'RX filter half-width', description: 'Channel filter cutoff around center — the filter and the freq/waterfall display span rebuild from it together. 50000 = the standard ±50 kHz (100 kHz channel). Stamped into capture metadata. Applies on radio start.', control: { kind: 'number', unit: 'Hz', value: cfg.platform.radio?.rx_bw ?? 50000, onChange: (v) => updatePlatform('radio', 'rx_bw', v) } },
        ]},
        { title: 'Capture', rows: [
          { id: 'iq_record', label: 'IQ recording', description: 'Record the RX stream as SigMF under <log dir>/iq (~1.6 MB/s). Applies on radio start.', control: { kind: 'toggle', value: cfg.platform.radio?.iq_record ?? false, onChange: (v) => updatePlatform('radio', 'iq_record', v) } },
          { id: 'iq_raw_record', label: 'Raw 1 Msps capture', description: 'Diagnostic pre-decimation recording for impulse/QRM analysis (~8 MB/s, 50 GB maximum ≈ 104 min). Applies on radio start.', control: { kind: 'toggle', value: cfg.platform.radio?.iq_raw_record ?? false, onChange: (v) => updatePlatform('radio', 'iq_raw_record', v) } },
          { id: 'iq_disk_reserve_gb', label: 'Disk reserve', description: 'Free space preserved when capture caps are planned; recording is reduced or disabled before decoding is put at risk.', control: { kind: 'number', unit: 'GB', value: cfg.platform.radio?.iq_disk_reserve_gb ?? 10, onChange: (v) => updatePlatform('radio', 'iq_disk_reserve_gb', v) } },
        ]},
      ],
    })

    const identifier = cfg.platform.tracking?.tle_fetch?.identifier ?? ''
    const provider = cfg.platform.tracking?.tle_fetch?.provider ?? 'celestrak'
    const autoFetchRows: SettingRow[] = [
      { id: 'provider', label: 'Provider', description: 'Which catalog the Fetch button queries.', control: { kind: 'select', value: provider, options: [{ value: 'celestrak', label: 'CelesTrak' }, { value: 'spacetrack', label: 'Space-Track' }], onChange: (v) => updateTrackingFetch({ provider: v as 'celestrak' | 'spacetrack' }) } },
    ]
    if (provider === 'spacetrack') {
      autoFetchRows.push({
        id: 'creds', label: 'Space-Track credentials',
        control: { kind: 'env-status', rows: [
          { name: 'SPACETRACK_IDENTITY', present: !!credStatus?.identity_set },
          { name: 'SPACETRACK_PASSWORD', present: !!credStatus?.password_set },
        ], hint: 'Set both in the environment before launching MAV_WEB.py. Credentials are never typed into or stored by the app.' },
      })
    }
    autoFetchRows.push(
      { id: 'identifier', label: 'Catalog identifier', description: 'NORAD ID or name to look up.', control: { kind: 'fetch-identifier', value: identifier, onChange: (v) => updateTrackingFetch({ identifier: v }), onFetch: () => { void handleFetchTle() }, fetching, fetchMsg, disabled: fetching || !identifier.trim() } },
      { id: 'auto_refresh', label: 'Auto-refresh', description: 'Periodically re-fetch elements.', control: { kind: 'toggle', value: cfg.platform.tracking?.tle_fetch?.auto_refresh ?? false, onChange: (v) => updateTrackingFetch({ auto_refresh: v }) } },
      { id: 'refresh_hours', label: 'Refresh interval', description: 'How often to re-fetch while enabled.', control: { kind: 'number', unit: 'hours', value: cfg.platform.tracking?.tle_fetch?.refresh_interval_hours ?? 12, onChange: (v) => updateTrackingFetch({ refresh_interval_hours: v }) } },
    )
    const logCadence = cfg.platform.tracking?.control?.log_cadence ?? 'off'
    out.push({
      id: 'tracking', title: 'Tracking', description: 'Orbit propagation and Doppler tuning source.',
      groups: [
        { title: 'Element set', rows: [
          { id: 'tle_source', label: 'TLE source', description: 'Where the current elements came from.', control: { kind: 'info', value: cfg.platform.tracking?.tle?.source || (cfg.platform.tracking?.tle?.method === 'manual' ? 'Manual entry' : '—') } },
          { id: 'tle', label: 'Two-line elements (TLE)', description: 'Name, line 1, line 2 — applied live on the next tick.', control: { kind: 'tle', draft: tleDraft, onChange: handleTleDraftChange } },
        ]},
        { title: 'Auto-fetch', rows: autoFetchRows },
        { title: 'Diagnostic logging', rows: [
          { id: 'log_cadence', label: 'Background tracking sample rate', description: 'Every RX decode and TX attempt always logs its own az/el and requested Doppler-corrected frequencies, regardless of this setting. This only controls an additional background trace between those, for reviewing the dead air of a pass.', control: { kind: 'select', value: logCadence, options: [
            { value: 'off', label: 'Off (default) — RX/TX only' },
            { value: 'tx_throttled', label: 'Throttled background trace' },
            { value: 'tick', label: 'Full background trace (1 Hz)' },
          ], onChange: (v) => updateTrackingControl({ log_cadence: v as 'off' | 'tick' | 'tx_throttled' }) } },
          { id: 'log_decimation_s', label: 'Background sample interval', description: 'Minimum time between background samples while throttled. Ignored in "Off" and "Full background trace" modes.', control: { kind: 'number', unit: 's', value: cfg.platform.tracking?.control?.log_decimation_s ?? 5, onChange: (v) => updateTrackingControl({ log_decimation_s: v }) } },
        ]},
      ],
    })

    if (statusInfo) {
      const rows: SettingRow[] = [
        { id: 'version', label: 'Version', control: { kind: 'info', value: statusInfo.version } },
        { id: 'schema', label: 'Schema', control: { kind: 'info', value: (statusInfo.schema_path || '').split('/').pop() ?? '' } },
        { id: 'commands', label: 'Commands', control: { kind: 'info', value: String(statusInfo.schema_count) } },
        { id: 'log_dir', label: 'Log dir', control: { kind: 'info', value: statusInfo.log_dir } },
      ]
      if (statusInfo.session_log_json) {
        rows.push({ id: 'session_data', label: 'Session data', control: { kind: 'info', value: statusInfo.session_log_json.split('/').pop() ?? '' } })
      }
      out.push({ id: 'about', title: 'About', description: 'Session and build details.', groups: [{ title: 'Session', rows }] })
    }
    return out
  }, [cfg, tleDraft, statusInfo, credStatus, fetching, fetchMsg, missions, switchError, handleTleDraftChange, handleFetchTle, updateMission, updateMissionTopLevel, updatePlatform, updateRadioFrequency, updateTrackingControl, updateTrackingFetch, updateTrackingTle])

  const trimmed = search.trim()
  const contentPanes = trimmed ? panes : panes.filter((p) => p.id === activeCategory)

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ backgroundColor: colors.modalBackdrop }}
          initial={animateOnMount ? { opacity: 0 } : false}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          onClick={handleCancel}
        >
          <motion.div
            ref={panelRef}
            onClick={(e) => e.stopPropagation()}
            className="w-[760px] max-w-[94vw] max-h-[84vh] rounded-lg border shadow-overlay flex flex-col overflow-hidden"
            style={{ backgroundColor: colors.bgPanelRaised, borderColor: colors.borderStrong }}
            initial={animateOnMount ? { scale: 0.98, opacity: 0 } : false}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.98, opacity: 0 }}
            transition={springConfig}
          >
            <div className="flex items-center justify-between px-4 py-3 border-b" style={{ borderColor: colors.borderSubtle }}>
              <div className="flex items-center gap-2">
                <Settings className="size-4" style={{ color: colors.label }} />
                <span className="text-sm font-bold" style={{ color: colors.label }}>Configuration</span>
              </div>
              <div className="flex items-center gap-2">
                <Kbd>Esc</Kbd>
                <button onClick={handleCancel} className="p-1 rounded hover:bg-white/5" aria-label="Close">
                  <X className="size-4" style={{ color: colors.dim }} />
                </button>
              </div>
            </div>

            {!cfg ? (
              <div className="p-4 text-xs" style={{ color: colors.dim }}>Loading…</div>
            ) : (
              <>
                <div className="flex flex-1 min-h-0">
                  <ConfigRail
                    items={RAIL_ITEMS}
                    activeId={activeCategory}
                    searching={!!trimmed}
                    onSelect={(id) => { setSearch(''); setActiveCategory(id) }}
                    search={search}
                    onSearch={setSearch}
                  />
                  <div className="flex-1 overflow-y-auto p-4 min-w-0">
                    <SearchContext.Provider value={trimmed}>
                      {contentPanes.map((pane) => <PaneRenderer key={pane.id} pane={pane} />)}
                      {trimmed && !panes.some((p) => p.groups.some((g) => g.rows.some((r) => matchesQuery(trimmed, r.label, r.description)))) && (
                      <div className="text-xs" style={{ color: colors.dim }}>No settings match “{trimmed}”.</div>
                    )}
                    </SearchContext.Provider>
                  </div>
                </div>

                <div className="flex items-center justify-between px-4 py-3 border-t" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
                  <div className="flex items-center gap-2 text-xs" style={{ color: colors.dim }}>
                    {dirty && (
                      <>
                        <span className="inline-block size-1.5 rounded-full" style={{ backgroundColor: colors.active }} />
                        Unsaved changes
                      </>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <button onClick={handleCancel} className="px-3.5 py-1.5 rounded text-xs border" style={{ color: colors.dim, borderColor: colors.borderSubtle }}>
                      Cancel
                    </button>
                    <button
                      onClick={() => { void handleSave().then((saved) => { if (saved) onClose() }) }}
                      disabled={!dirty}
                      className="px-4 py-1.5 rounded text-xs font-bold disabled:opacity-30 btn-feedback"
                      style={{ backgroundColor: dirty ? colors.success : colors.borderSubtle, color: colors.bgApp }}
                    >
                      Save &amp; Close
                    </button>
                  </div>
                </div>
              </>
            )}
          </motion.div>

          <ConfirmDialog
            open={pendingSwitch !== null}
            title="Switch mission?"
            detail={pendingSwitch
              ? `Switches to ${pendingSwitch.name}, stops the radio process, and restarts the server. The page reloads when the new mission is up.`
              : ''}
            variant="caution"
            onCancel={() => setPendingSwitch(null)}
            onConfirm={() => { void handleConfirmSwitch() }}
          />

          {switchingTo && (
            <div
              className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3"
              style={{ backgroundColor: `${colors.bgApp}F5` }}
              onClick={(e) => e.stopPropagation()}
            >
              <span className="text-sm font-bold" style={{ color: colors.label }}>
                Switching to {switchingTo.name}
              </span>
              <span className="text-xs" style={{ color: colors.dim }}>
                Server restarting — the page reloads when the new mission is up…
              </span>
              <button
                onClick={() => window.location.reload()}
                className="mt-2 px-3.5 py-1.5 rounded text-xs border"
                style={{ color: colors.label, borderColor: colors.borderStrong }}
              >
                RELOAD
              </button>
            </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
