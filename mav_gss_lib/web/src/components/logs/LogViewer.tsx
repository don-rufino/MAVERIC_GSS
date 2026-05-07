import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  X, ChevronDown, ChevronRight, AlertTriangle, Binary,
  ArrowUpFromLine, ClipboardCopy, Braces, Cpu,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Calendar } from '@/components/ui/calendar'
import { Separator } from '@/components/ui/separator'
import { colors } from '@/lib/colors'
import { useLogQuery, type LogEntry } from '@/hooks/useLogQuery'
import {
  ContextMenuRoot, ContextMenuTrigger, ContextMenuContent,
  ContextMenuItem,
} from '@/components/shared/overlays/ContextMenu'
import { LogFilterBar } from './LogFilterBar'

interface LogViewerProps {
  open: boolean
  onClose: () => void
}

function parseSessionLabel(sid: string): { date: string; time: string; label: string } {
  const m = sid.match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/)
  if (m) {
    const date = `${m[1]}-${m[2]}-${m[3]}`
    const time = `${m[4]}:${m[5]}:${m[6]}`
    const prefix = sid.split('_')[0] ?? ''
    return { date, time, label: prefix }
  }
  return { date: '', time: '', label: sid }
}

function hhmmss(ts_iso: string | undefined, ts_ms: number | undefined): string {
  if (ts_iso) {
    const m = ts_iso.match(/T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,3}))?/)
    if (m) return m[2] ? `${m[1]}.${m[2].padEnd(3, '0')}` : m[1]
  }
  if (typeof ts_ms === 'number' && ts_ms > 0) {
    const d = new Date(ts_ms)
    const hh = String(d.getUTCHours()).padStart(2, '0')
    const mm = String(d.getUTCMinutes()).padStart(2, '0')
    const ss = String(d.getUTCSeconds()).padStart(2, '0')
    const ms = String(d.getUTCMilliseconds()).padStart(3, '0')
    return `${hh}:${mm}:${ss}.${ms}`
  }
  return '--:--:--'
}

const SYSTEM_EVENT_KINDS = new Set(['tracking', 'radio'])

function isSystemEvent(kind: string): boolean {
  return SYSTEM_EVENT_KINDS.has(kind)
}

function systemEventBlock(e: LogEntry, kind: string): Record<string, unknown> | undefined {
  const block = e[kind]
  return block && typeof block === 'object' ? block as Record<string, unknown> : undefined
}

function systemEventAction(e: LogEntry, kind: string): string {
  const block = systemEventBlock(e, kind)
  const action = block?.action
  return typeof action === 'string' ? action : ''
}

function systemEventState(e: LogEntry, kind: string): string {
  const block = systemEventBlock(e, kind)
  if (!block) return ''
  // Tracking carries `mode`; radio carries `state`. Either way, surface the
  // post-action condition in the frame column so the row is scannable.
  const mode = block.mode
  if (typeof mode === 'string' && mode) return mode
  const state = block.state
  if (typeof state === 'string' && state) return state
  return ''
}

function entryLabel(e: LogEntry): string {
  const kind = String(e.event_kind ?? '')
  if (isSystemEvent(kind)) {
    const action = systemEventAction(e, kind)
    return action ? `${kind}.${action}` : kind
  }
  const mission = e.mission
  if (mission && typeof mission === 'object') {
    const missionObj = mission as Record<string, unknown>
    const missionCmd = missionObj.cmd_id
    if (typeof missionCmd === 'string' && missionCmd) return missionCmd

    const facts = missionObj.facts
    if (facts && typeof facts === 'object') {
      const header = (facts as Record<string, unknown>).header
      if (header && typeof header === 'object') {
        const cmdId = (header as Record<string, unknown>).cmd_id
        if (typeof cmdId === 'string' && cmdId) return cmdId
      }
    }
  }
  return ''
}

function formatHexBlock(hex: string): string {
  return hex.match(/.{1,2}/g)?.join(' ') ?? ''
}

function isBinaryJunk(s: string): boolean {
  return Array.from(s).some((ch) => {
    const code = ch.charCodeAt(0)
    return code === 0xfffd
      || (code < 0x20 && code !== 0x09 && code !== 0x0a && code !== 0x0d)
      || (code >= 0x7f && code <= 0x9f)
  })
}
function toHexLabel(s: string): string {
  let hex = ''
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i)
    const byte = code === 0xfffd ? 0xff : code & 0xff
    hex += byte.toString(16).padStart(2, '0')
  }
  return `<bytes:0x${hex}>`
}
function sanitizeForDisplay(value: unknown): unknown {
  if (typeof value === 'string') return isBinaryJunk(value) ? toHexLabel(value) : value
  if (Array.isArray(value)) return value.map(sanitizeForDisplay)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = sanitizeForDisplay(v)
    }
    return out
  }
  return value
}

function prettyMissionJson(value: unknown): string {
  return JSON.stringify(sanitizeForDisplay(value), null, 2)
}


const springConfig = { type: 'spring' as const, stiffness: 500, damping: 30, mass: 0.8 }
let hasLoadedLogViewer = false


export function LogViewer({ open, onClose }: LogViewerProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<Element | null>(null)
  const [expandedSet, setExpandedSet] = useState<Set<number>>(new Set())
  const animateOnMount = hasLoadedLogViewer

  const {
    sessions,
    selected,
    setSelected,
    entries,
    telemetryByParent,
    loading,
    hasMore,
    currentOffset,
    error,
    labelFilter,
    setLabelFilter,
    fromTime,
    setFromTime,
    toTime,
    setToTime,
    dateFilter,
    setDateFilter,
    fetchSessions,
    fetchEntries,
    reset,
  } = useLogQuery()

  useEffect(() => { hasLoadedLogViewer = true }, [])

  useEffect(() => {
    if (open) {
      triggerRef.current = document.activeElement
    } else if (triggerRef.current && triggerRef.current instanceof HTMLElement) {
      triggerRef.current.focus()
      triggerRef.current = null
    }
  }, [open])

  const handleTab = useCallback((e: KeyboardEvent) => {
    if (e.key !== 'Tab' || !panelRef.current) return
    const focusable = panelRef.current.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )
    if (focusable.length === 0) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (e.shiftKey) {
      if (document.activeElement === first) { e.preventDefault(); last.focus() }
    } else {
      if (document.activeElement === last) { e.preventDefault(); first.focus() }
    }
  }, [])

  useEffect(() => {
    if (!open) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose() }
      if (e.key === 'Tab') handleTab(e)
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [open, onClose, handleTab])

  useEffect(() => {
    if (open && panelRef.current) {
      const btn = panelRef.current.querySelector<HTMLElement>('button')
      btn?.focus()
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    fetchSessions()
  }, [open, fetchSessions])

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (selected) { setExpandedSet(new Set()); fetchEntries(selected, false, 0) }
  }, [selected, fetchEntries])

  useEffect(() => {
    if (!open) { setExpandedSet(new Set()); reset() }
  }, [open, reset])
  /* eslint-enable react-hooks/set-state-in-effect */


  // Prefer the date embedded in the session_id filename over file mtime —
  // mtime reflects last-write time, which can lag far behind the session
  // capture time when files are copied, restored, or touched.
  const sessionDateCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const s of sessions) {
      const parsed = parseSessionLabel(s.session_id).date
      const ds = parsed || (s.mtime
        ? new Date(s.mtime * 1000).toLocaleDateString('en-CA')
        : '')
      if (ds) counts[ds] = (counts[ds] || 0) + 1
    }
    return counts
  }, [sessions])

  const sessionDates = useMemo(() => {
    return Object.keys(sessionDateCounts).map(d => new Date(d + 'T00:00:00'))
  }, [sessionDateCounts])

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex"
          initial={animateOnMount ? { opacity: 0 } : false}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
        >
          <motion.div
            className="absolute inset-0 frosted-backdrop"
            style={{ backgroundColor: colors.modalBackdrop }}
          />

          <motion.div
            ref={panelRef}
            className="flex flex-1 m-4 rounded-lg border overflow-hidden shadow-overlay relative"
            style={{ backgroundColor: colors.bgPanelRaised, borderColor: colors.borderStrong }}
            initial={animateOnMount ? { opacity: 0, scale: 0.95 } : false}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.95 }}
            transition={springConfig}
          >
            {/* Left sidebar: calendar + session list */}
            <div className="w-72 shrink-0 border-r flex flex-col overflow-hidden" style={{ borderColor: colors.borderSubtle }}>
              <div className="flex items-center justify-between px-3 py-2 border-b" style={{ borderColor: colors.borderSubtle }}>
                <span className="text-xs font-bold uppercase tracking-wider" style={{ color: colors.label }}>Sessions</span>
                <div className="flex items-center gap-1">
                  {dateFilter && (
                    <button onClick={() => setDateFilter('')} className="text-[11px] px-1.5 py-0.5 rounded hover:bg-white/5" style={{ color: colors.dim }}>
                      Clear
                    </button>
                  )}
                  <button onClick={onClose} className="p-1 rounded hover:bg-white/5">
                    <X className="size-3.5" style={{ color: colors.dim }} />
                  </button>
                </div>
              </div>
              <div className="shrink-0 border-b flex justify-center" style={{ borderColor: colors.borderSubtle }}>
                <Calendar
                  mode="single"
                  selected={dateFilter ? new Date(dateFilter + 'T00:00:00') : undefined}
                  onSelect={(day) => setDateFilter(day ? day.toLocaleDateString('en-CA') : '')}
                  modifiers={{ hasSession: sessionDates }}
                  modifiersClassNames={{ hasSession: 'log-day-marker' }}
                  className="!p-2 !bg-transparent w-full [--cell-size:--spacing(6)]"
                />
              </div>
              <div className="flex-1 overflow-y-auto">
                {sessions.length === 0 ? (
                  <div className="px-3 py-4 text-xs text-center" style={{ color: colors.dim }}>No sessions found</div>
                ) : (
                  sessions.filter((s) => {
                    if (!dateFilter) return true
                    const parsed = parseSessionLabel(s.session_id).date
                    const ds = parsed || (s.mtime
                      ? new Date(s.mtime * 1000).toLocaleDateString('en-CA')
                      : '')
                    return ds === dateFilter
                  }).map((s) => {
                    const sid = s.session_id
                    const isSession = s.direction === 'session'
                    const parsed = parseSessionLabel(sid)
                    let dateStr = parsed.date
                    let timeStr2 = parsed.time
                    if ((!dateStr || !timeStr2) && s.mtime) {
                      const d = new Date(s.mtime * 1000)
                      if (!dateStr) dateStr = d.toLocaleDateString('en-CA')
                      if (!timeStr2) timeStr2 = d.toLocaleTimeString('en-GB', { hour12: false })
                    }
                    const sizeKb = typeof s.size === 'number' ? (s.size / 1024).toFixed(1) + ' KB' : '?'
                    const isSel = selected === sid
                    const dirColor = isSession ? colors.label : colors.dim
                    const DirIcon = isSession ? Braces : ArrowUpFromLine
                    const tagMatch = sid.match(/\d{8}_\d{6}_(.+?)(?:\.jsonl)?$/)
                    const tag = tagMatch ? tagMatch[1] : ''
                    return (
                      <div
                        key={sid}
                        className="flex items-center border-b transition-colors hover:bg-white/5"
                        style={{ borderColor: colors.borderSubtle, backgroundColor: isSel ? `${colors.label}11` : 'transparent', borderLeft: `2px solid ${isSel ? dirColor : 'transparent'}` }}
                      >
                        <button
                          onClick={() => setSelected(sid)}
                          className="flex-1 text-left px-2 py-1.5 min-w-0"
                        >
                          <div className="flex items-center gap-1.5">
                            <DirIcon className="size-3 shrink-0" style={{ color: dirColor }} />
                            <span className="text-[11px] font-bold uppercase shrink-0" style={{ color: dirColor }}>
                              Session
                            </span>
                            <span className="text-[11px] font-mono tabular-nums" style={{ color: isSel ? colors.label : colors.value }}>
                              {dateStr} {timeStr2}
                            </span>
                            <span className="text-[11px]" style={{ color: colors.dim }}>{sizeKb}</span>
                          </div>
                          {tag && <div className="text-[11px] truncate pl-5" style={{ color: colors.sep }}>{tag}</div>}
                          <div className="text-[11px] font-mono truncate pl-5" style={{ color: colors.sep }}>{sid}</div>
                        </button>
                      </div>
                    )
                  })
                )}
              </div>
            </div>

            {/* Right area */}
            <div className="flex-1 flex flex-col overflow-hidden">
              <LogFilterBar
                labelFilter={labelFilter}
                fromTime={fromTime}
                toTime={toTime}
                entryCount={entries.length}
                hasSelection={!!selected}
                onLabelFilterChange={setLabelFilter}
                onFromTimeChange={setFromTime}
                onToTimeChange={setToTime}
              />

              {error && (
                <div className="px-3 py-1.5 text-[11px] shrink-0" style={{ color: colors.danger, backgroundColor: `${colors.danger}0a` }}>
                  {error}
                </div>
              )}

              {/* Column headers */}
              {selected && entries.length > 0 && (
                <div className="flex items-center text-[11px] font-light px-2 py-0.5 shrink-0" style={{ color: colors.sep }}>
                  <span className="w-5 px-1" />
                  <span className="px-2 w-12 text-right">#</span>
                  <span className="px-2 w-28">time</span>
                  <span className="px-2 w-16">kind</span>
                  <span className="px-2 flex-1">label</span>
                  <span className="px-2 w-24">frame</span>
                  <span className="px-2 w-16 text-right">size</span>
                  <span className="px-2 w-16 text-right">inner</span>
                  <span className="px-2 w-16">flags</span>
                </div>
              )}

              {/* Entries */}
              <div className="flex-1 overflow-y-auto">
                {!selected ? (
                  <div className="flex items-center justify-center h-full text-xs" style={{ color: colors.dim }}>Select a session</div>
                ) : loading ? (
                  <div className="flex items-center justify-center h-full text-xs" style={{ color: colors.dim }}>Loading...</div>
                ) : entries.length === 0 ? (
                  <div className="flex items-center justify-center h-full text-xs" style={{ color: colors.dim }}>No matching entries</div>
                ) : (
                  <>
                  {entries.map((e, i) => {
                    const kind = String(e.event_kind ?? '?')
                    const isTx = kind === 'tx_command'
                    const isSys = isSystemEvent(kind)
                    const seq = Number(e.seq ?? 0)
                    const timeStr = hhmmss(e.ts_iso as string | undefined, e.ts_ms as number | undefined)
                    const label = entryLabel(e)
                    const frame = isSys
                      ? systemEventState(e, kind)
                      : String(e.frame_label ?? e.frame_type ?? '')
                    const innerLen = Number(e.inner_len ?? e.size ?? 0)
                    const wireLen = Number(e.wire_len ?? innerLen)
                    const rawLen = innerLen  // legacy alias used below
                    const innerHex = String(e.inner_hex ?? e.raw_hex ?? '')
                    const wireHex = String(e.wire_hex ?? (isTx ? innerHex : ''))
                    const rawHex = innerHex  // legacy alias used below
                    const displayLen = isSys ? '' : (isTx ? wireLen : rawLen)
                    const displayInnerLen = isTx && innerHex ? innerLen : ''
                    const warnings = (Array.isArray(e.warnings) ? e.warnings : []) as string[]
                    const eventId = String(e.event_id ?? '')
                    const fragments = (eventId ? telemetryByParent.get(eventId) : undefined) ?? []
                    const isDup = !!e.duplicate
                    const isEcho = !!e.uplink_echo
                    const isUnknown = !!e.unknown
                    const isExpanded = expandedSet.has(i)
                    const dirColor = isSys ? colors.info : (isTx ? colors.label : colors.success)
                    const kindLabel = isSys ? 'SYS' : (isTx ? 'TX' : 'RX')
                    const mission = (e.mission && typeof e.mission === 'object') ? e.mission as Record<string, unknown> : undefined
                    const sysBlock = isSys ? systemEventBlock(e, kind) : undefined

                    return (
                      <ContextMenuRoot key={i}>
                        <ContextMenuTrigger>
                          <div>
                            {/* Row */}
                            <div
                              className="flex items-center px-3 py-1 text-xs font-mono cursor-pointer hover:bg-white/[0.03]"
                              style={{ backgroundColor: isExpanded ? `${colors.label}08` : undefined }}
                              onClick={() => setExpandedSet(prev => { const next = new Set(prev); if (next.has(i)) next.delete(i); else next.add(i); return next })}
                            >
                              {isExpanded ? <ChevronDown className="size-3 shrink-0" style={{ color: colors.label }} /> : <ChevronRight className="size-3 shrink-0" style={{ color: colors.dim }} />}
                              <span className="px-2 w-12 text-right tabular-nums" style={{ color: colors.value }}>{seq}</span>
                              <span className="px-2 w-28 tabular-nums" style={{ color: colors.value }}>{timeStr}</span>
                              <span className="px-2 w-16 uppercase font-bold text-[10px] flex items-center gap-1" style={{ color: dirColor }}>
                                {isSys && <Cpu className="size-3 shrink-0" />}
                                {kindLabel}
                              </span>
                              <span className="px-2 flex-1 truncate" style={{ color: colors.label }}>{label || (isUnknown ? '(unknown)' : '')}</span>
                              <span className="px-2 w-24 truncate" style={{ color: colors.dim }}>{frame}</span>
                              <span className="px-2 w-16 text-right tabular-nums" style={{ color: colors.dim }}>{displayLen}</span>
                              <span className="px-2 w-16 text-right tabular-nums" style={{ color: colors.dim }}>{displayInnerLen}</span>
                              <span className="px-2 w-16 flex items-center gap-1">
                                {isDup && <Badge className="text-[9px] h-4 px-1" style={{ backgroundColor: `${colors.warning}22`, color: colors.warning }}>DUP</Badge>}
                                {isEcho && <Badge className="text-[9px] h-4 px-1" style={{ backgroundColor: `${colors.info}22`, color: colors.info }}>UL</Badge>}
                                {warnings.length > 0 && <AlertTriangle className="size-3" style={{ color: colors.warning }} />}
                              </span>
                            </div>

                            {/* Expanded detail */}
                            {isExpanded && (
                              <div className="px-6 py-2 space-y-1.5" style={{ backgroundColor: colors.bgApp }}>
                                {warnings.length > 0 && (
                                  <div className="flex items-center gap-1 flex-wrap">
                                    <AlertTriangle className="size-3" style={{ color: colors.warning }} />
                                    {warnings.map((w, wi) => (
                                      <Badge key={wi} className="text-[11px] h-5" style={{ backgroundColor: `${colors.warning}22`, color: colors.warning }}>{w}</Badge>
                                    ))}
                                  </div>
                                )}

                                {isSys && sysBlock && (
                                  <details open>
                                    <summary className="text-[11px] cursor-pointer select-none" style={{ color: colors.sep }}>
                                      {kind}
                                    </summary>
                                    <div className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px] font-mono">
                                      {Object.entries(sysBlock).map(([k, v]) => {
                                        if (v === null || v === undefined || v === '') return null
                                        const display = typeof v === 'object'
                                          ? JSON.stringify(sanitizeForDisplay(v))
                                          : String(v)
                                        return (
                                          <div key={k} className="contents">
                                            <span style={{ color: colors.dim }}>{k}</span>
                                            <span className="break-all" style={{ color: colors.value }}>{display}</span>
                                          </div>
                                        )
                                      })}
                                    </div>
                                  </details>
                                )}

                                {!isSys && fragments.length > 0 && (
                                  <details open>
                                    <summary className="text-[11px] cursor-pointer select-none" style={{ color: colors.sep }}>
                                      telemetry ({fragments.length})
                                    </summary>
                                    <div className="mt-1 grid grid-cols-[auto_auto_1fr_auto] gap-x-3 gap-y-0.5 text-[11px] font-mono">
                                      <span className="font-bold uppercase" style={{ color: colors.sep }}>domain</span>
                                      <span className="font-bold uppercase" style={{ color: colors.sep }}>key</span>
                                      <span className="font-bold uppercase" style={{ color: colors.sep }}>value</span>
                                      <span className="font-bold uppercase" style={{ color: colors.sep }}>unit</span>
                                      {fragments.map((frag, fi) => {
                                        const fullName = String(frag.name ?? '')
                                        const dot = fullName.indexOf('.')
                                        const dom = dot >= 0 ? fullName.slice(0, dot) : ''
                                        const k = dot >= 0 ? fullName.slice(dot + 1) : fullName
                                        const v = frag.value
                                        const unit = String(frag.unit ?? '')
                                        const display = typeof v === 'object' && v !== null
                                          ? JSON.stringify(sanitizeForDisplay(v))
                                          : String(v)
                                        return (
                                          <div key={fi} className="contents">
                                            <span style={{ color: colors.dim }}>{dom}</span>
                                            <span style={{ color: colors.label }}>{k}</span>
                                            <span className="truncate" style={{ color: colors.value }} title={display}>{display}</span>
                                            <span style={{ color: colors.dim }}>{unit}</span>
                                          </div>
                                        )
                                      })}
                                    </div>
                                  </details>
                                )}

                                {!isSys && mission && Object.keys(mission).length > 0 && (
                                  <details open>
                                    <summary className="text-[11px] cursor-pointer select-none" style={{ color: colors.sep }}>
                                      mission
                                    </summary>
                                    <pre className="text-[11px] p-2 rounded font-mono whitespace-pre-wrap break-all mt-1"
                                         style={{ color: colors.value, backgroundColor: 'rgba(0,0,0,0.3)' }}>
                                      {prettyMissionJson(mission)}
                                    </pre>
                                  </details>
                                )}

                                {isTx && innerHex && (
                                  <>
                                    <Separator style={{ backgroundColor: colors.borderSubtle }} />
                                    <div className="flex items-start gap-1">
                                      <Binary className="size-3 mt-0.5 shrink-0" style={{ color: colors.sep }} />
                                      <div className="flex-1">
                                        <div className="text-[10px] uppercase tracking-wider pb-1" style={{ color: colors.sep }}>inner ({innerLen}B)</div>
                                        <pre className="text-[11px] p-2 rounded font-mono whitespace-pre-wrap break-all" style={{ color: colors.dim, backgroundColor: 'rgba(0,0,0,0.3)' }}>
                                          {formatHexBlock(innerHex)}
                                        </pre>
                                      </div>
                                    </div>
                                  </>
                                )}

                                {!isSys && ((isTx && wireHex && wireHex !== innerHex) || (!isTx && rawHex)) && (
                                  <div className="flex items-start gap-1">
                                    <Binary className="size-3 mt-0.5 shrink-0" style={{ color: colors.sep }} />
                                    <div className="flex-1">
                                      <div className="text-[10px] uppercase tracking-wider pb-1" style={{ color: colors.sep }}>
                                        {isTx ? `wire (${wireLen}B)` : `raw (${rawLen}B)`}
                                      </div>
                                      <pre className="text-[11px] p-2 rounded font-mono whitespace-pre-wrap break-all" style={{ color: colors.dim, backgroundColor: 'rgba(0,0,0,0.3)' }}>
                                        {formatHexBlock(isTx ? wireHex : rawHex)}
                                      </pre>
                                    </div>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        </ContextMenuTrigger>
                        <ContextMenuContent>
                          <ContextMenuItem
                            icon={ClipboardCopy}
                            onSelect={() => navigator.clipboard.writeText(label)}
                          >
                            Copy Label
                          </ContextMenuItem>
                          {!isSys && (
                            <ContextMenuItem
                              icon={Braces}
                              onSelect={() => mission && navigator.clipboard.writeText(prettyMissionJson(mission))}
                            >
                              Copy Mission JSON
                            </ContextMenuItem>
                          )}
                          {isSys && sysBlock && (
                            <ContextMenuItem
                              icon={Braces}
                              onSelect={() => navigator.clipboard.writeText(prettyMissionJson(sysBlock))}
                            >
                              Copy {kind} JSON
                            </ContextMenuItem>
                          )}
                          {isTx && innerHex && (
                            <ContextMenuItem
                              icon={Binary}
                              onSelect={() => navigator.clipboard.writeText(innerHex)}
                            >
                              Copy Inner Hex
                            </ContextMenuItem>
                          )}
                          {isTx && wireHex && (
                            <ContextMenuItem
                              icon={Binary}
                              onSelect={() => navigator.clipboard.writeText(wireHex)}
                            >
                              Copy Wire Hex
                            </ContextMenuItem>
                          )}
                          {!isTx && rawHex && (
                            <ContextMenuItem
                              icon={Binary}
                              onSelect={() => navigator.clipboard.writeText(rawHex)}
                            >
                              Copy Raw Hex
                            </ContextMenuItem>
                          )}
                        </ContextMenuContent>
                      </ContextMenuRoot>
                    )
                  })}
                  {hasMore && (
                    <div className="flex justify-center py-3">
                      <button
                        onClick={() => selected && fetchEntries(selected, true, currentOffset)}
                        disabled={loading}
                        className="text-xs px-4 py-1.5 rounded border hover:bg-white/5 disabled:opacity-40"
                        style={{ color: colors.label, borderColor: colors.borderSubtle }}
                      >
                        {loading ? 'Loading...' : 'Load more'}
                      </button>
                    </div>
                  )}
                  </>
                )}
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
