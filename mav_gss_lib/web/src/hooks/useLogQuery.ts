import { useState, useCallback } from 'react'
import { useDebouncedValue } from './useDebouncedValue'

export type LogEntry = Record<string, unknown>
const PAGE_SIZE = 200

export interface LogSession {
  session_id: string
  filename: string
  size: number
  mtime: number
  direction: 'session' | 'unknown'
}

export function useLogQuery() {
  const [sessions, setSessions] = useState<LogSession[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [telemetryByParent, setTelemetryByParent] = useState<Map<string, LogEntry[]>>(new Map())
  const [trackingSamples, setTrackingSamples] = useState<LogEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [currentOffset, setCurrentOffset] = useState(0)
  const [error, setError] = useState<string | null>(null)

  const [labelFilter, setLabelFilter] = useState('')
  const [fromTime, setFromTime] = useState('')
  const [toTime, setToTime] = useState('')
  const [dateFilter, setDateFilter] = useState('')

  const debouncedLabel = useDebouncedValue(labelFilter, 300)
  const debouncedFrom = useDebouncedValue(fromTime, 300)
  const debouncedTo = useDebouncedValue(toTime, 300)

  const fetchSessions = useCallback(() => {
    fetch('/api/logs')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data: LogSession[]) => {
        setSessions(data)
        setError(null)
      })
      .catch((e) => setError(`Failed to load sessions: ${String(e)}`))
  }, [])

  const fetchEntries = useCallback((sessionId: string, append = false, offsetOverride = 0) => {
    setLoading(true)
    const params = new URLSearchParams()
    if (debouncedLabel) params.set('label', debouncedLabel)
    if (debouncedFrom) params.set('from', debouncedFrom)
    if (debouncedTo) params.set('to', debouncedTo)
    params.set('offset', String(offsetOverride))
    params.set('limit', String(PAGE_SIZE))
    fetch(`/api/logs/${sessionId}?${params.toString()}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data: { entries: LogEntry[]; has_more: boolean }) => {
        setEntries(prev => append ? [...prev, ...data.entries] : data.entries)
        setHasMore(data.has_more)
        setCurrentOffset(offsetOverride + data.entries.length)
        setLoading(false)
        setError(null)
      })
      .catch((e) => {
        if (!append) setEntries([])
        setHasMore(false)
        setCurrentOffset(0)
        setLoading(false)
        setError(`Failed to load entries: ${String(e)}`)
      })
    // Fetch parameter events for this session once per selection, grouped
    // by rx_event_id so the viewer can show fragments under each packet.
    // Cheap — the `/parameters` endpoint filters at the file level and
    // caps at 10 000 rows (one session's worth).
    if (!append) {
      fetch(`/api/logs/${sessionId}/parameters?limit=10000`)
        .then((r) => r.ok ? r.json() : { entries: [] })
        .then((data: { entries: LogEntry[] }) => {
          const map = new Map<string, LogEntry[]>()
          for (const t of data.entries) {
            const parent = t.rx_event_id
            if (typeof parent === 'string') {
              if (!map.has(parent)) map.set(parent, [])
              map.get(parent)!.push(t)
            }
          }
          setTelemetryByParent(map)
        })
        .catch(() => setTelemetryByParent(new Map()))

      // Exact-capture tracking samples (source=rx_decode / tx_attempt) for
      // joining az/el + Doppler into the RX/TX row that triggered them.
      // Excludes background "tick" rows even on a session with the
      // background trace opted in — those aren't attached to any packet
      // and have no row to join into.
      fetch(`/api/logs/${sessionId}/tracking_samples?source=rx_decode,tx_attempt&limit=10000`)
        .then((r) => r.ok ? r.json() : { entries: [] })
        .then((data: { entries: LogEntry[] }) => setTrackingSamples(data.entries))
        .catch(() => setTrackingSamples([]))
    }
  }, [debouncedLabel, debouncedFrom, debouncedTo])

  const reset = useCallback(() => {
    setSelected(null)
    setEntries([])
    setTelemetryByParent(new Map())
    setTrackingSamples([])
    setLabelFilter('')
    setFromTime('')
    setToTime('')
    setDateFilter('')
    setHasMore(false)
    setCurrentOffset(0)
    setError(null)
  }, [])

  return {
    sessions,
    selected,
    setSelected,
    entries,
    telemetryByParent,
    trackingSamples,
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
    debouncedLabel,
    debouncedFrom,
    debouncedTo,
    fetchSessions,
    fetchEntries,
    reset,
  }
}
