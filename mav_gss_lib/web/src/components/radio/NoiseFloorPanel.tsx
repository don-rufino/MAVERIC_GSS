import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { Activity } from 'lucide-react'
import { colors } from '@/lib/colors'
import {
  ROW, START_CAUSE_TEXT, STATE, coverage, gainMarkers, heldSeconds, mmss, passLabel, radioName, recentQuiet, spansOf, utcTime,
  type GainStats, type NoiseFloorPass, type PassRadio, type SeriesRow,
} from '@/lib/noiseFloor'

interface NoiseFloorPanelProps {
  /** The pass to show: the live one, or the latest logged one. */
  pass: NoiseFloorPass | null
  live: boolean
  /** Logged passes, newest first (summaries). Omit to hide the table. */
  recent?: NoiseFloorPass[]
  nextAosMs?: number | null
  /** Radio tab (default) or a record opened from the Logs view. */
  source?: 'radio' | 'log'
  /** Station-wide serial -> name list. */
  radioNames?: Record<string, string>
  /** USRP of the current (or last) flowgraph run, and whether that run is still going. */
  usrp?: PassRadio | null
  usrpRunning?: boolean
}

const mono = 'JetBrains Mono, ui-monospace, monospace'
const f1 = (v: number | null | undefined) => (v === null || v === undefined ? '--' : v.toFixed(1))
const gainText = (g: number | null) => (g === null ? '--' : `${g} dB`)

function Badge({ tone, children, title, dot = true }: { tone: string; children: ReactNode; title?: string; dot?: boolean }) {
  return (
    <span title={title} className="inline-flex h-5 items-center gap-1.5 whitespace-nowrap rounded border px-1.5 text-[11px] font-semibold"
      style={{ color: tone, borderColor: `${tone}66` }}>
      {dot && <span className="size-1.5 rounded-full" style={{ backgroundColor: tone }} />}
      {children}
    </span>
  )
}

function Cell({ label, children, tone }: { label: string; children: ReactNode; tone?: string }) {
  return (
    <div className="min-w-0 py-1">
      <div className="truncate text-[11px] font-medium uppercase" style={{ color: colors.textMuted }}>{label}</div>
      <div className="mt-0.5 truncate font-mono text-xs tabular-nums" style={{ color: tone ?? colors.textPrimary }}>{children}</div>
    </div>
  )
}

const Small = ({ children }: { children: ReactNode }) => <small className="text-[11px]" style={{ color: colors.textMuted }}>{children}</small>

function currentGain(pass: NoiseFloorPass): GainStats | null {
  const series = pass.series ?? []
  const last = series[series.length - 1]
  return pass.by_gain.find(g => last && g.gain_db === last[ROW.gain]) ?? pass.by_gain[pass.by_gain.length - 1] ?? null
}

function StateBadges({ pass, live, nextAosMs, source }: { pass: NoiseFloorPass | null; live: boolean; nextAosMs?: number | null; source: 'radio' | 'log' }) {
  if (!live || !pass) {
    return (
      <span className="flex items-center gap-1.5">
        {pass && !pass.complete && (
          <Badge tone={colors.warning} dot={false} title="The flowgraph stopped before LOS; the record covers the seconds before it stopped.">INCOMPLETE</Badge>
        )}
        {source === 'radio' && <Badge tone={colors.neutral}>IDLE{nextAosMs ? ` · next AOS ${utcTime(nextAosMs)}` : ''}</Badge>}
      </span>
    )
  }
  const last = pass.series?.[pass.series.length - 1]
  if (!last) return <Badge tone={colors.success}>MEASURING</Badge>
  const held = last[ROW.satEl] > pass.el_max_deg
  const state = last[ROW.state]
  return (
    <span className="flex items-center gap-1.5">
      {held && <Badge tone={colors.info} dot={false} title={`Satellite at ${last[ROW.satEl].toFixed(1)}°; the antenna holds ${pass.el_max_deg}° and follows in azimuth`}>EL HELD {pass.el_max_deg}°</Badge>}
      {state === STATE.adjusting && <Badge tone={colors.textSecondary}>GAIN ADJUSTING · {gainText(last[ROW.gain])}</Badge>}
      {state === STATE.burst && <Badge tone={colors.warning}>BURST +{f1(last[ROW.burst])} dB</Badge>}
      {state === STATE.tx && <Badge tone={colors.neutral}>TRANSMITTING</Badge>}
      {state === STATE.quiet && <Badge tone={colors.success}>MEASURING</Badge>}
    </span>
  )
}

function CoverageCell({ pass }: { pass: NoiseFloorPass }) {
  const cov = coverage(pass)
  if (!cov) return <Cell label="Coverage">--</Cell>
  const cause = pass.start_cause ? START_CAUSE_TEXT[pass.start_cause] : undefined
  return (
    <div className="min-w-0 py-1" title={cause}>
      <div className="truncate text-[11px] font-medium uppercase" style={{ color: colors.textMuted }}>Coverage</div>
      <div className="mt-0.5 truncate font-mono text-xs" style={{ color: cov.kind === 'full' ? colors.success : colors.warning }}>
        {cov.text}{cov.percent !== null && cov.kind !== 'full' && <> <Small>{cov.percent} %</Small></>}
      </div>
    </div>
  )
}

/** Recorded max elevation, plus the predicted peak when the record missed it. */
function peakNote(pass: NoiseFloorPass): string | null {
  const peak = pass.predicted?.max_el_deg
  if (peak == null || pass.sat_max_el_deg == null || peak <= pass.sat_max_el_deg + 0.5) return null
  return `peak ${peak.toFixed(0)}°`
}

function Cells({ pass, live }: { pass: NoiseFloorPass; live: boolean }) {
  const series = pass.series ?? []
  const last = series[series.length - 1]
  const g = currentGain(pass)
  const at = g ? ` @ ${gainText(g.gain_db)}` : ''
  const gains = pass.by_gain.map(b => b.gain_db).join(' → ')
  return (
    <div className="grid grid-cols-3 gap-x-4 gap-y-1 px-3 py-2 @[760px]:grid-cols-4 @[1000px]:grid-cols-6">
      <Cell label="RX gain">
        {live && last
          ? <>{gainText(last[ROW.gain])} <Small>{last[ROW.state] === STATE.adjusting ? 'adjusting' : `held ${mmss(heldSeconds(series))}`}</Small></>
          : `${gains || '--'}${gains ? ' dB' : ''}`}
      </Cell>
      <Cell label={`Quiet floor${at}`}>{f1(g?.quiet_floor_dbfs)} <Small>dBFS</Small></Cell>
      <Cell label="Spread p10–p90">{g?.p10_dbfs == null ? '--' : `${f1(g.p10_dbfs)} … ${f1(g.p90_dbfs)}`}</Cell>
      <Cell label="Band busy" tone={g && g.busy_frac > 0.1 ? colors.warning : undefined}>{g ? `${Math.round(g.busy_frac * 100)} %` : '--'}</Cell>
      <Cell label="Strongest burst">{g?.max_burst_db == null ? '--' : `+${f1(g.max_burst_db)} dB`}</Cell>
      <Cell label={`Plain avg${at}`} tone={colors.textSecondary}>{f1(g?.plain_avg_dbfs)} <Small>dBFS</Small></Cell>
      <Cell label={`Counted${at}`}>{g ? `${g.counted_s} s` : '--'}</Cell>
      <Cell label="Adjusting" tone={colors.textSecondary}>{pass.adjusting_s} s <Small>not counted</Small></Cell>
      {live && last ? (
        <>
          <Cell label="Now (10 s)">
            {f1(recentQuiet(series, last[ROW.gain]))} <Small>{last[ROW.state] === STATE.adjusting ? 'adjusting' : 'dBFS'}</Small>
          </Cell>
          <Cell label="Elevation">
            {last[ROW.satEl] > pass.el_max_deg
              ? <>{last[ROW.antEl].toFixed(0)}° <Small>sat {last[ROW.satEl].toFixed(1)}°</Small></>
              : `${last[ROW.antEl].toFixed(1)}°`}
          </Cell>
          <Cell label="Azimuth">{last[ROW.az].toFixed(0)}°</Cell>
        </>
      ) : (
        <>
          <Cell label="Pass">{utcTime(pass.aos_ms)} – {utcTime(pass.los_ms)}</Cell>
          <Cell label="Max elevation">
            {f1(pass.sat_max_el_deg)}°{pass.held_at_max_s > 0 && <> <Small>held {pass.el_max_deg}° {mmss(pass.held_at_max_s)}</Small></>}
            {peakNote(pass) && <> <Small>{peakNote(pass)}</Small></>}
          </Cell>
          <CoverageCell pass={pass} />
        </>
      )}
      <Cell label="Gains this pass">{gains ? `${gains} dB` : '--'}</Cell>
    </div>
  )
}

/** Chart drawn at its container's real pixel width, so 11 px labels stay
 *  11 px at any panel width (a scaled fixed-width drawing would shrink them). */
function Sized({ fallback, children }: { fallback: number; children: (width: number) => ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(fallback)
  useEffect(() => {
    const node = ref.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => {
      const w = Math.round(entry.contentRect.width)
      if (w > 0) setWidth(w)
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])
  return <div ref={ref} className="min-w-0">{children(width)}</div>
}

function TimeChart({ pass, live, W }: { pass: NoiseFloorPass; live: boolean; W: number }) {
  const hatch = `nf-hatch-${useId().replace(/:/g, '')}`
  const H = 230, L = 44, R = 34, T = 10, B = 26
  const series = pass.series ?? []
  const lastT = series.length ? series[series.length - 1][ROW.t] : 0
  const endMs = live ? pass.predicted_los_ms ?? pass.los_ms : pass.los_ms
  const dur = Math.max(lastT, (endMs - pass.aos_ms) / 1000, 60)
  const floors = series.map(r => r[ROW.floor]).filter((v): v is number => v !== null)
  const y0 = floors.length ? Math.floor(Math.min(...floors)) - 1 : -105
  const y1 = floors.length ? Math.min(Math.ceil(Math.max(...floors)) + 1, y0 + 24) : -95
  const X = (t: number) => L + (t / dur) * (W - L - R)
  const Y = (d: number) => T + ((y1 - Math.max(y0, Math.min(y1, d))) / (y1 - y0)) * (H - T - B)
  const E = (e: number) => T + ((90 - e) / 90) * (H - T - B)
  const yStep = y1 - y0 > 12 ? 4 : 2
  const yTicks: number[] = []
  for (let d = Math.ceil(y0 / yStep) * yStep; d <= y1; d += yStep) yTicks.push(d)
  const minStep = dur > 900 ? 5 : dur > 360 ? 2 : 1
  const xTicks: number[] = []
  for (let m = 0; m * 60 <= dur; m += minStep) xTicks.push(m)
  const path = (pred: (r: SeriesRow) => boolean) => {
    let d = '', pen = false, prevT = -2
    for (const r of series) {
      const on = pred(r) && r[ROW.floor] !== null
      if (!on) { pen = false; continue }
      d += `${pen && r[ROW.t] - prevT <= 2 ? 'L' : 'M'}${X(r[ROW.t]).toFixed(1)} ${Y(r[ROW.floor]!).toFixed(1)}`
      pen = true
      prevT = r[ROW.t]
    }
    return d
  }
  const held = spansOf(series, r => r[ROW.satEl] > pass.el_max_deg)
  const adjusting = spansOf(series, r => r[ROW.state] === STATE.adjusting)
  const markers = gainMarkers(series)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="block" role="img"
      aria-label={`Noise floor each second over the pass${pass.by_gain.length ? `, quiet floor ${pass.by_gain.map(g => `${f1(g.quiet_floor_dbfs)} dBFS at ${gainText(g.gain_db)}`).join(', ')}` : ''}`}>
      <defs>
        <pattern id={hatch} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="5" stroke={colors.textSecondary} strokeOpacity={0.25} strokeWidth={2} />
        </pattern>
      </defs>
      {held.map(([a, z]) => (
        <g key={`h${a}`}>
          <rect x={X(a)} y={T} width={Math.max(1, X(z) - X(a))} height={E(pass.el_max_deg) - T} fill={colors.info} fillOpacity={0.07} />
          <text x={(X(a) + X(z)) / 2} y={T + 13} fill={colors.info} fontSize={11} fontFamily={mono} textAnchor="middle">
            ▲ sat above {pass.el_max_deg}° · antenna held at {pass.el_max_deg}°
          </text>
        </g>
      ))}
      {yTicks.map(d => (
        <g key={`y${d}`}>
          <line x1={L} x2={W - R} y1={Y(d)} y2={Y(d)} stroke={colors.borderSubtle} />
          <text x={L - 6} y={Y(d) + 4} fill={colors.textDisabled} fontSize={11} fontFamily={mono} textAnchor="end">{d}</text>
        </g>
      ))}
      {[0, 30, pass.el_max_deg, 90].filter((e, i, a) => a.indexOf(e) === i).map(e => (
        <text key={`e${e}`} x={W - R + 5} y={E(e) + 4} fill={colors.info} fillOpacity={0.8} fontSize={11} fontFamily={mono}>{e}°</text>
      ))}
      {xTicks.map(m => (
        <text key={`x${m}`} x={X(m * 60)} y={H - 8} fill={colors.textDisabled} fontSize={11} fontFamily={mono} textAnchor="middle">{m}m</text>
      ))}
      <polyline points={series.map(r => `${X(r[ROW.t]).toFixed(1)},${E(r[ROW.satEl]).toFixed(1)}`).join(' ')}
        fill="none" stroke={colors.info} strokeOpacity={0.35} strokeWidth={1} strokeDasharray="2 3" />
      <polyline points={series.map(r => `${X(r[ROW.t]).toFixed(1)},${E(r[ROW.antEl]).toFixed(1)}`).join(' ')}
        fill="none" stroke={colors.info} strokeOpacity={0.6} strokeWidth={1.2} strokeDasharray="5 3" />
      {adjusting.map(([a, z]) => (
        <rect key={`a${a}`} x={X(a) - 1} y={T} width={X(z) - X(a) + 3} height={H - T - B} fill={`url(#${hatch})`} />
      ))}
      <path d={path(r => r[ROW.state] === STATE.quiet)} fill="none" stroke={colors.active} strokeWidth={1.2} strokeLinejoin="round" />
      <path d={path(r => r[ROW.state] === STATE.adjusting)} fill="none" stroke={colors.textMuted} strokeWidth={1.2} />
      {series.filter(r => r[ROW.state] === STATE.burst && r[ROW.floor] !== null).map(r => (
        <line key={`b${r[ROW.t]}`} x1={X(r[ROW.t])} x2={X(r[ROW.t])}
          y1={Y(r[ROW.floor]! - (r[ROW.burst] ?? 0) + pass.burst_threshold_db)} y2={Y(r[ROW.floor]!)}
          stroke={colors.warning} strokeWidth={1.5} />
      ))}
      {pass.by_gain.filter(g => g.spans_s.length).map(g => {
        const a = X(g.spans_s[0][0]), z = X(g.spans_s[g.spans_s.length - 1][1])
        return (
          <g key={`g${g.gain_db}`}>
            {g.quiet_floor_dbfs !== null && (
              <>
                <line x1={a} x2={z} y1={Y(g.quiet_floor_dbfs)} y2={Y(g.quiet_floor_dbfs)} stroke={colors.textPrimary} strokeDasharray="6 3" />
                <text x={a + 4} y={Y(g.quiet_floor_dbfs) - 5} fill={colors.textPrimary} fontSize={11} fontFamily={mono}>quiet {f1(g.quiet_floor_dbfs)}</text>
              </>
            )}
            {g.plain_avg_dbfs !== null && g.quiet_floor_dbfs !== null && g.plain_avg_dbfs - g.quiet_floor_dbfs > 0.05 && (
              <>
                <line x1={a} x2={z} y1={Y(g.plain_avg_dbfs)} y2={Y(g.plain_avg_dbfs)} stroke={colors.neutral} strokeDasharray="1 3" />
                <text x={z - 4} y={Y(g.plain_avg_dbfs) - 5} fill={colors.textMuted} fontSize={11} fontFamily={mono} textAnchor="end">plain avg {f1(g.plain_avg_dbfs)}</text>
              </>
            )}
          </g>
        )
      })}
      {markers.map(({ t, gain }) => {
        const x = X(t), label = `RX ${gainText(gain)}`
        return (
          <g key={`m${t}`}>
            {t > 0 && <line x1={x} x2={x} y1={T} y2={H - B} stroke={colors.textPrimary} strokeWidth={1.2} />}
            <rect x={x + 2} y={H - B - 18} width={label.length * 6.8 + 8} height={15} rx={2} fill={colors.bgPanel} stroke={colors.textPrimary} strokeOpacity={0.6} />
            <text x={x + 6} y={H - B - 7} fill={colors.textPrimary} fontSize={11} fontFamily={mono}>{label}</text>
          </g>
        )
      })}
      <text x={12} y={(T + H - B) / 2} fill={colors.textMuted} fontSize={11} textAnchor="middle" transform={`rotate(-90 12 ${(T + H - B) / 2})`}>dBFS / bin</text>
    </svg>
  )
}

function ElevationChart({ pass, W }: { pass: NoiseFloorPass; W: number }) {
  const H = 230, L = 44, R = 10, T = 22, B = 26
  const lo = pass.el_min_deg, hi = pass.el_max_deg
  const values = pass.by_gain.flatMap(g => g.by_elevation.flatMap(b => [b[3] ?? b[1], b[4] ?? b[1]]))
  const y0 = values.length ? Math.floor(Math.min(...values)) - 1 : -105
  const y1 = values.length ? Math.ceil(Math.max(...values)) + 1 : -97
  const X = (e: number) => L + ((e - lo) / Math.max(1, hi - lo)) * (W - L - R)
  const Y = (d: number) => T + ((y1 - Math.max(y0, Math.min(y1, d))) / (y1 - y0)) * (H - T - B)
  const step = y1 - y0 > 10 ? 2 : 1
  const yTicks: number[] = []
  for (let d = Math.ceil(y0 / step) * step; d <= y1; d += step) yTicks.push(d)
  const xTicks: number[] = []
  for (let e = lo; e <= hi; e += 10) xTicks.push(e)
  const series = pass.series ?? []
  const curGain = series.length ? series[series.length - 1][ROW.gain] : pass.by_gain[pass.by_gain.length - 1]?.gain_db
  const n = pass.by_gain.length
  let keyX = W - R - 4
  const key = [...pass.by_gain].reverse().map(g => {
    const label = gainText(g.gain_db), x = keyX
    keyX -= label.length * 6.8 + 22
    return { g, label, x }
  })
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="block" role="img" aria-label="Quiet floor by antenna elevation in 5 degree steps, per RX gain">
      {yTicks.map(d => (
        <g key={`y${d}`}>
          <line x1={L} x2={W - R} y1={Y(d)} y2={Y(d)} stroke={colors.borderSubtle} />
          <text x={L - 6} y={Y(d) + 4} fill={colors.textDisabled} fontSize={11} fontFamily={mono} textAnchor="end">{d}</text>
        </g>
      ))}
      {xTicks.map(e => (
        <text key={`x${e}`} x={X(e)} y={H - 8} fill={colors.textDisabled} fontSize={11} fontFamily={mono} textAnchor="middle">{e}°</text>
      ))}
      {pass.by_gain.map((g, gi) => {
        const isCur = g.gain_db === curGain, col = isCur ? colors.active : colors.textSecondary
        const dx = n > 1 ? (gi - (n - 1) / 2) * 7 : 0
        return g.by_elevation.map(([elLo, mean, count, p10, p90]) => {
          const x = X(Math.min(hi, elLo + 2.5)) + dx
          return (
            <g key={`${g.gain_db}-${elLo}`}>
              <line x1={x} x2={x} y1={Y(p90 ?? mean)} y2={Y(p10 ?? mean)} stroke={col} strokeOpacity={0.4} strokeWidth={4} strokeLinecap="round" />
              <circle cx={x} cy={Y(mean)} r={3.2} fill={isCur ? col : colors.bgPanel} stroke={col} strokeWidth={1.4}>
                <title>{`RX ${gainText(g.gain_db)} · ${elLo}–${elLo + 5}°: ${f1(mean)} dBFS (${count} s)`}</title>
              </circle>
            </g>
          )
        })
      })}
      {key.map(({ g, label, x }) => {
        const isCur = g.gain_db === curGain, col = isCur ? colors.active : colors.textSecondary
        return (
          <g key={`k${g.gain_db}`}>
            <text x={x} y={14} fill={col} fontSize={11} fontFamily={mono} textAnchor="end">{label}</text>
            <circle cx={x - label.length * 6.8 - 8} cy={10} r={3.2} fill={isCur ? col : colors.bgPanel} stroke={col} strokeWidth={1.4} />
          </g>
        )
      })}
      {!pass.by_gain.length && (
        <text x={(L + W - R) / 2} y={H / 2} fill={colors.textMuted} fontSize={11} textAnchor="middle">
          waiting for a gain held {pass.gain_hold_s} s
        </text>
      )}
    </svg>
  )
}

function Legend() {
  const item = (style: React.CSSProperties, label: string, swatch = false) => (
    <span key={label}>
      <i className={`mr-1.5 inline-block align-middle ${swatch ? 'h-[9px] w-[9px]' : 'w-3.5'}`} style={style} />{label}
    </span>
  )
  return (
    <div className="flex flex-wrap gap-x-3.5 gap-y-1 px-3.5 pb-2 pt-1 text-[11.5px]" style={{ color: colors.textMuted }}>
      {item({ borderTop: `2px solid ${colors.active}` }, 'Floor each second')}
      {item({ borderTop: `2px solid ${colors.warning}` }, 'Burst second (excluded)')}
      {item({ borderTop: `1px dashed ${colors.textPrimary}` }, 'Quiet floor (per gain)')}
      {item({ borderTop: `2px dotted ${colors.neutral}` }, 'Plain average (per gain)')}
      {item({ borderLeft: `1.5px solid ${colors.textPrimary}`, height: 12, width: 2 }, 'RX gain set (held ≥ 5 s)', true)}
      {item({ background: `repeating-linear-gradient(45deg, ${colors.textSecondary}40 0 2px, transparent 2px 5px)` }, 'Gain adjusting (not counted)', true)}
      {item({ borderTop: `1px dashed ${colors.info}` }, 'Antenna elevation')}
      {item({ borderTop: `1px dotted ${colors.info}`, opacity: 0.6 }, 'Satellite elevation (TLE)')}
      {item({ background: `${colors.info}1f` }, 'Antenna held at max elevation', true)}
    </div>
  )
}

type RadioFilter = 'all' | 'radio'

function RecentPasses({ rows, names, usrp, usrpRunning }: {
  rows: { pass: NoiseFloorPass; current: boolean; live: boolean }[]
  names: Record<string, string>
  usrp: PassRadio | null
  usrpRunning: boolean
}) {
  const [filter, setFilter] = useState<RadioFilter>('all')
  // the attached radio while the flowgraph runs; otherwise the radio of the newest pass that has one
  const filterRadio = usrp?.serial ? usrp : rows.find(r => r.pass.radio?.serial)?.pass.radio ?? null
  const filterLabel = filterRadio ? `${radioName(filterRadio, names)} (${usrp?.serial && usrpRunning ? 'attached' : 'last used'})` : null
  const shown = filter === 'radio' && filterRadio ? rows.filter(r => r.pass.radio?.serial === filterRadio.serial) : rows
  const hidden = rows.length - shown.length
  const body = shown.flatMap(({ pass, current, live }) => {
    const gains: (GainStats | null)[] = pass.by_gain.length ? pass.by_gain : [null]
    const day = pass.sun_el_deg !== null && pass.sun_el_deg >= 0
    const name = radioName(pass.radio, names)
    const cov = live ? null : coverage(pass)
    const peak = peakNote(pass)
    return gains.map((g, i) => (
      <tr key={`${pass.aos_ms}-${i}`} style={{ color: current ? colors.active : colors.textPrimary }}>
        <td className="pt-1 pr-3 text-left">{i === 0 ? `${passLabel(pass.aos_ms)}${live ? ' (live)' : ''}` : ''}</td>
        <td className="pt-1 pr-3 text-left"
          style={{ color: !name ? colors.textDisabled : current ? undefined : pass.radio?.serial === usrp?.serial ? undefined : colors.textSecondary }}
          title={i === 0 ? (pass.radio ? `Ettus ${pass.radio.product || 'USRP'} · serial ${pass.radio.serial}` : 'Recorded before radio serials were logged') : undefined}>
          {i === 0 ? name ?? '—' : ''}
        </td>
        <td className="pt-1 pr-3 text-left" title={i === 0 && pass.start_cause ? START_CAUSE_TEXT[pass.start_cause] : undefined}>
          {i === 0 && (live ? <span style={{ color: colors.textMuted }}>recording</span> : cov ? (
            <span style={{ color: current ? undefined : cov.kind === 'full' ? colors.success : colors.warning }}>
              {cov.text}{cov.percent !== null && cov.kind !== 'full' && <span style={{ color: colors.textMuted }}> {cov.percent} %</span>}
            </span>
          ) : '--')}
        </td>
        <td className="pt-1 pr-3 text-left" style={{ color: day ? colors.warning : colors.info }}
          title={pass.sun_el_deg === null ? undefined : `Sun ${pass.sun_el_deg.toFixed(0)}° at max elevation`}>
          {i === 0 && pass.sun_el_deg !== null ? (day ? '☀ day' : '☾ night') : ''}
        </td>
        <td className="pt-1 pr-3 text-right">
          {i === 0 && (
            <span className="inline-flex items-center justify-end gap-1.5">
              {f1(pass.sat_max_el_deg)}°
              {pass.sat_max_el_deg !== null && pass.sat_max_el_deg > pass.el_max_deg && (
                <Badge tone={colors.info} dot={false}>▲ held {pass.el_max_deg}°</Badge>
              )}
              {peak && <span style={{ color: colors.textMuted }}>{peak}</span>}
            </span>
          )}
        </td>
        <td className="pt-1 pr-3 text-right">{g ? gainText(g.gain_db) : '--'}</td>
        <td className="pt-1 pr-3 text-right">{f1(g?.quiet_floor_dbfs)}</td>
        <td className="pt-1 pr-3 text-right">{g?.p10_dbfs == null ? '--' : `${f1(g.p10_dbfs)} … ${f1(g.p90_dbfs)}`}</td>
        <td className="pt-1 pr-3 text-right">{g ? `${Math.round(g.busy_frac * 100)} %` : '--'}</td>
        <td className="pt-1 text-right">{g ? `${g.counted_s} s` : '--'}</td>
      </tr>
    ))
  })
  const segButton = (value: RadioFilter, label: string) => (
    <button type="button" key={value} onClick={() => setFilter(value)} aria-pressed={filter === value}
      className="border-r px-2 py-0.5 font-mono text-[11px] last:border-r-0"
      style={{
        borderColor: colors.borderStrong,
        color: filter === value ? colors.textPrimary : colors.textMuted,
        backgroundColor: filter === value ? colors.borderSubtle : 'transparent',
        boxShadow: filter === value ? `inset 0 -2px 0 ${colors.active}` : undefined,
      }}>
      {label}
    </button>
  )
  return (
    <div className="overflow-x-auto px-3 pb-3">
      <div className="flex flex-wrap items-center justify-between gap-2 pb-1 pt-1">
        <span className="text-[11px] font-semibold uppercase tracking-[0.06em]" style={{ color: colors.textMuted }}>Recent passes</span>
        {filterLabel && (
          <span className="inline-flex overflow-hidden rounded-sm border" role="group" aria-label="Show passes from" style={{ borderColor: colors.borderStrong }}>
            {segButton('all', 'All radios')}
            {segButton('radio', filterLabel)}
          </span>
        )}
      </div>
      <table className="w-full border-collapse whitespace-nowrap font-mono text-xs tabular-nums">
        <thead>
          <tr className="border-b" style={{ borderColor: colors.borderSubtle }}>
            {['Pass (UTC)', 'Radio', 'Coverage', 'Light', 'Max el', 'RX gain', 'Quiet floor', 'p10–p90', 'Busy', 'Counted'].map((h, i) => (
              <th key={h} className={`pb-1 pr-3 font-sans text-[11px] font-normal ${i < 4 ? 'text-left' : 'text-right'}`}
                style={{ color: colors.textDisabled }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
      {hidden > 0 && (
        <div className="pt-1.5 text-[11px]" style={{ color: colors.textMuted }}>
          {hidden} pass{hidden > 1 ? 'es' : ''} from other radios or with no serial hidden
        </div>
      )}
    </div>
  )
}

export function NoiseFloorPanel({ pass, live, recent, nextAosMs, source = 'radio', radioNames, usrp, usrpRunning }: NoiseFloorPanelProps) {
  // the running radio's name is live (a rename reaches it at once); the list may lag until the next refresh
  const names = useMemo(
    () => (usrp?.serial && usrp.name ? { ...radioNames, [usrp.serial]: usrp.name } : radioNames ?? {}),
    [radioNames, usrp],
  )
  const passRadio = pass ? radioName(pass.radio, names) : null
  const rows: { pass: NoiseFloorPass; current: boolean; live: boolean }[] = []
  if (recent) {
    if (live && pass) rows.push({ pass, current: true, live: true })
    for (const r of recent) rows.push({ pass: r, current: !live && !!pass && r.aos_ms === pass.aos_ms, live: false })
  }
  return (
    <section className="@container flex shrink-0 flex-col rounded-lg border shadow-panel" aria-label="Noise floor"
      style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
      <div className="flex min-h-[33px] shrink-0 items-center justify-between gap-3 border-b px-3 py-1.5" style={{ borderColor: colors.borderSubtle }}>
        <div className="flex min-w-0 items-center gap-2">
          <Activity className="size-3.5 shrink-0" style={{ color: colors.dim }} />
          <span className="truncate text-xs font-bold uppercase tracking-wide" style={{ color: colors.value }}>Noise Floor</span>
          {pass && (
            <span className="truncate text-xs" style={{ color: colors.textMuted }}>
              · {live || source === 'log' ? '' : 'last pass · '}{pass.satellite} {passLabel(pass.aos_ms)}
              {passRadio && <span title={pass.radio ? `Ettus ${pass.radio.product || 'USRP'} · serial ${pass.radio.serial}` : undefined}> · {passRadio}</span>}
            </span>
          )}
        </div>
        <StateBadges pass={pass} live={live} nextAosMs={nextAosMs} source={source} />
      </div>
      {pass ? (
        <>
          <Cells pass={pass} live={live} />
          <div className="grid grid-cols-1 gap-1 px-1.5 pb-1 @[760px]:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
            <div className="min-w-0">
              <div className="px-1.5 pt-1 text-[11px] font-semibold uppercase tracking-[0.06em]" style={{ color: colors.textMuted }}>Floor over the pass</div>
              <Sized fallback={640}>{(w) => <TimeChart pass={pass} live={live} W={w} />}</Sized>
            </div>
            <div className="min-w-0">
              <div className="px-1.5 pt-1 text-[11px] font-semibold uppercase tracking-[0.06em]" style={{ color: colors.textMuted }}>Quiet floor by antenna elevation</div>
              <Sized fallback={380}>{(w) => <ElevationChart pass={pass} W={w} />}</Sized>
            </div>
          </div>
          <Legend />
        </>
      ) : (
        <p className="m-0 px-3.5 py-3 text-xs" style={{ color: colors.textMuted }}>
          No pass recorded yet. While the flowgraph runs, every pass of the tracked satellite is recorded automatically
          from the station's minimum elevation{nextAosMs ? `; next AOS ${utcTime(nextAosMs)}` : ''}.
        </p>
      )}
      {rows.length > 0 && <RecentPasses rows={rows} names={names} usrp={usrp ?? null} usrpRunning={!!usrpRunning} />}
    </section>
  )
}
