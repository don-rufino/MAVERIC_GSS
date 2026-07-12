import { useCallback, useEffect, useRef, useState } from 'react'
import { authFetch } from '@/lib/auth'
import { createSocket } from '@/lib/ws'

export interface StreamHealth {
  rms_dbfs: number
  peak_dbfs: number
  clip_count: number
  overflows_total: number
  span_s: number
  ts_ms: number
  stale: boolean
}

export interface TxHealth {
  underflows_total: number
  time_errors_total: number
  seq_errors_total: number
  ts_ms: number
  stale: boolean
}

export interface RadioStatus {
  enabled: boolean
  autostart: boolean
  state: 'stopped' | 'running' | 'stopping' | 'crashed'
  running: boolean
  pid: number | null
  started_at_ms: number | null
  uptime_s: number
  last_runtime_s: number
  exit_code: number | null
  error: string
  script: string
  cwd: string
  command: string[]
  stop_timeout_s: number
  log_lines: number
  stream_health?: StreamHealth | null
  tx_health?: TxHealth | null
  log_file?: string | null
}

export const DEFAULT_STATUS: RadioStatus = {
  enabled: false,
  autostart: false,
  state: 'stopped',
  running: false,
  pid: null,
  started_at_ms: null,
  uptime_s: 0,
  last_runtime_s: 0,
  exit_code: null,
  error: '',
  script: '',
  cwd: '',
  command: [],
  stop_timeout_s: 8.0,
  log_lines: 1000,
}

interface LogLine {
  id: number
  text: string
}

export interface UseRadioSocket {
  status: RadioStatus
  logs: LogLine[]
  connected: boolean
  lastUpdateMs: number
  busy: 'start' | 'stop' | 'restart' | null
  actionError: string | null
  runAction: (action: 'start' | 'stop' | 'restart') => Promise<void>
  dismissError: () => void
}

export function useRadioSocket(): UseRadioSocket {
  const [status, setStatus] = useState<RadioStatus>(DEFAULT_STATUS)
  const [logs, setLogs] = useState<LogLine[]>([])
  const [connected, setConnected] = useState(false)
  const [busy, setBusy] = useState<'start' | 'stop' | 'restart' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [lastUpdateMs, setLastUpdateMs] = useState<number>(Date.now())
  const logIdRef = useRef(0)
  const logLimitRef = useRef(DEFAULT_STATUS.log_lines)

  useEffect(() => {
    logLimitRef.current = status.log_lines || DEFAULT_STATUS.log_lines
  }, [status.log_lines])

  // Re-poll every 5s only while the websocket is disconnected. The WS
  // pushes status messages on every state change, so once connected
  // the poll is redundant; firing it on every reconnect would also
  // race the WS push and could overwrite fresher state with a stale
  // HTTP response. Seed via tick() only on the disconnected path —
  // the WS itself emits the seed status the moment it opens.
  useEffect(() => {
    if (connected) return
    let cancelled = false
    const tick = async () => {
      try {
        const r = await authFetch('/api/radio/status', { cache: 'no-store' })
        if (!r.ok) return
        const data = (await r.json()) as RadioStatus
        if (cancelled) return
        setStatus({ ...DEFAULT_STATUS, ...data })
        setLastUpdateMs(Date.now())
      } catch { /* ignore */ }
    }
    tick()
    const id = window.setInterval(tick, 5000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [connected])

  useEffect(() => {
    const sock = createSocket('/ws/radio', (data) => {
      const msg = data as Record<string, unknown>
      if (msg.type === 'status' && msg.status && typeof msg.status === 'object') {
        setStatus({ ...DEFAULT_STATUS, ...(msg.status as RadioStatus) })
        setLastUpdateMs(Date.now())
      } else if (msg.type === 'logs' && Array.isArray(msg.lines)) {
        setLogs(msg.lines.map((line) => ({ id: ++logIdRef.current, text: String(line) })))
        setLastUpdateMs(Date.now())
      } else if (msg.type === 'log') {
        const text = String(msg.line ?? '')
        const id = ++logIdRef.current
        setLogs((prev) => {
          const limit = logLimitRef.current
          const next = [...prev, { id, text }]
          return next.length > limit ? next.slice(-limit) : next
        })
        setLastUpdateMs(Date.now())
      } else if (msg.type === 'exit' && msg.status && typeof msg.status === 'object') {
        setStatus({ ...DEFAULT_STATUS, ...(msg.status as RadioStatus) })
        setLastUpdateMs(Date.now())
      }
    }, setConnected)

    return () => { sock.close() }
  }, [])

  const runAction = useCallback(async (action: 'start' | 'stop' | 'restart') => {
    setActionError(null)
    setBusy(action)
    try {
      const response = await authFetch(`/api/radio/${action}`, { method: 'POST' })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        const err = (data && typeof data === 'object' && 'error' in data) ? String(data.error) : `${action} failed (${response.status})`
        setActionError(err)
        if (data && typeof data === 'object' && 'status' in data && data.status && typeof data.status === 'object') {
          setStatus({ ...DEFAULT_STATUS, ...(data.status as RadioStatus) })
          setLastUpdateMs(Date.now())
        }
        return
      }
      setStatus({ ...DEFAULT_STATUS, ...(data as RadioStatus) })
      setLastUpdateMs(Date.now())
    } catch (e) {
      setActionError(e instanceof Error ? e.message : `${action} failed`)
    } finally {
      setBusy(null)
    }
  }, [])

  const dismissError = useCallback(() => setActionError(null), [])

  return { status, logs, connected, lastUpdateMs, busy, actionError, runAction, dismissError }
}
