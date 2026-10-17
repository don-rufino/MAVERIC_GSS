/** Per-pass noise floor records (server/radio/noise_floor.py). */

/** One second: [t_s, sat_el, ant_el, az, gain_db, floor_dbfs, state, burst_db]. */
export type SeriesRow = [number, number, number, number, number | null, number | null, number, number | null]

export const ROW = { t: 0, satEl: 1, antEl: 2, az: 3, gain: 4, floor: 5, state: 6, burst: 7 } as const
export const STATE = { quiet: 0, burst: 1, adjusting: 2, tx: 3 } as const

/** [el_lo, quiet_floor_dbfs, seconds, p10_dbfs, p90_dbfs] per 5° step of antenna elevation. */
export type ElevationStep = [number, number, number, number?, number?]

export interface GainStats {
  gain_db: number | null
  spans_s: [number, number][]
  counted_s: number
  quiet_s: number
  quiet_floor_dbfs: number | null
  p10_dbfs: number | null
  p90_dbfs: number | null
  plain_avg_dbfs: number | null
  busy_frac: number
  max_burst_db: number | null
  by_elevation: ElevationStep[]
}

export interface NoiseFloorPass {
  satellite: string
  station_id: string
  aos_ms: number
  los_ms: number
  predicted_los_ms?: number | null
  complete: boolean
  el_min_deg: number
  el_max_deg: number
  sat_max_el_deg: number | null
  held_at_max_s: number
  sun_el_deg: number | null
  gain_hold_s: number
  burst_threshold_db: number
  quiet_window_s: number
  rbw_hz: number | null
  seconds: number
  adjusting_s: number
  tx_s: number
  gain_changes: [number, number][]
  by_gain: GainStats[]
  series?: SeriesRow[]
  event_id?: string
  session_id?: string
}

export interface NoiseFloorSnapshot {
  active: NoiseFloorPass | null
  last: NoiseFloorPass | null
  recent: NoiseFloorPass[]
  next_aos_ms?: number | null
}

export const EMPTY_SNAPSHOT: NoiseFloorSnapshot = { active: null, last: null, recent: [] }

/** Live update: the summary so far plus the last few rows, whose state can
 *  still change (an adjusting stretch resolves once a gain holds). Returns
 *  null when the rows before the tail are missing (joined mid-pass): the
 *  caller refetches the snapshot instead. */
export function mergeLive(
  active: NoiseFloorPass | null,
  summary: NoiseFloorPass,
  tail: SeriesRow[],
): NoiseFloorPass | null {
  const known = active && active.aos_ms === summary.aos_ms ? active.series ?? [] : []
  const firstTail = tail.length ? tail[0][ROW.t] : 0
  const kept = known.filter(r => r[ROW.t] < firstTail)
  // one row per reported second: anything else means rows went missing
  if (kept.length + tail.length !== summary.seconds) return null
  return { ...summary, series: [...kept, ...tail] }
}

/** Contiguous [first t_s, last t_s] runs of rows matching `pred`. */
export function spansOf(series: SeriesRow[], pred: (r: SeriesRow) => boolean): [number, number][] {
  const out: [number, number][] = []
  for (const r of series) {
    if (!pred(r)) continue
    const last = out[out.length - 1]
    if (last && r[ROW.t] - last[1] <= 1) last[1] = r[ROW.t]
    else out.push([r[ROW.t], r[ROW.t]])
  }
  return out
}

/** Where each counted gain began: one marker per change of gain between
 *  counted seconds (transmit gaps at the same gain do not repeat it). */
export function gainMarkers(series: SeriesRow[]): { t: number; gain: number | null }[] {
  const out: { t: number; gain: number | null }[] = []
  let prev: number | null | undefined
  for (const r of series) {
    if (r[ROW.state] !== STATE.quiet && r[ROW.state] !== STATE.burst) continue
    if (prev === undefined || r[ROW.gain] !== prev) out.push({ t: r[ROW.t], gain: r[ROW.gain] })
    prev = r[ROW.gain]
  }
  return out
}

/** Quiet floor (power mean) of the last `n` quiet seconds at `gain`. */
export function recentQuiet(series: SeriesRow[], gain: number | null, n = 10): number | null {
  const vals: number[] = []
  for (let i = series.length - 1; i >= 0 && vals.length < n; i--) {
    const r = series[i]
    if (r[ROW.state] === STATE.quiet && r[ROW.gain] === gain && r[ROW.floor] !== null) vals.push(r[ROW.floor]!)
  }
  if (!vals.length) return null
  return 10 * Math.log10(vals.reduce((s, v) => s + 10 ** (v / 10), 0) / vals.length)
}

/** Seconds the current gain has held at the end of the series, 0 while adjusting. */
export function heldSeconds(series: SeriesRow[]): number {
  const last = series[series.length - 1]
  if (!last || last[ROW.state] === STATE.adjusting) return 0
  let n = 0
  for (let i = series.length - 1; i >= 0; i--) {
    const r = series[i]
    if (r[ROW.state] === STATE.adjusting || r[ROW.gain] !== last[ROW.gain]) break
    n++
  }
  return n
}

const pad = (v: number) => String(v).padStart(2, '0')

/** "10-16 18:42Z" */
export function passLabel(ms: number): string {
  const d = new Date(ms)
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`
}

/** "18:42Z" */
export function utcTime(ms: number): string {
  const d = new Date(ms)
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`
}

export function mmss(s: number): string {
  return `${pad(Math.floor(s / 60))}:${pad(Math.floor(s % 60))}`
}
