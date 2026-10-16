import { useState } from 'react'
import { colors } from '@/lib/colors'
import { MARGIN_LABEL, floorNote, isMeasured, marginTone, signed } from '@/lib/signal'
import { formatMhz, hasNominalOffset } from '@/lib/frequencyOffset'
import type { BandwidthLevel, SignalMeasurement } from '@/lib/types'

type SpectrumAxis = 'tuned' | 'nominal'

interface SignalCardProps {
  /** This packet's own beacon measurement. */
  signal: SignalMeasurement
  /** Required downlink Eb/N0 from the link budget; 0/undefined hides margin. */
  requiredEbn0Db?: number
}

/** Levels below peak beside −3 dB, drawn solid then dashed. */
const BW_LEVELS = [['10', ''], ['20', '6 3']] as const

function bwLevels(s: SignalMeasurement): [string, string, BandwidthLevel][] {
  return BW_LEVELS.flatMap(([db, dash]) => {
    const lv = s.bw_levels?.[db]
    return lv ? [[db, dash, lv] as [string, string, BandwidthLevel]] : []
  })
}

const W = 600
const H = 300
const L = 46
const R = 10
const T = 12
const B = 34

function Spectrum({ s, axis }: { s: SignalMeasurement; axis: SpectrumAxis }) {
  const spec = s.spectrum!
  const n = spec.db10.length
  const tune = hasNominalOffset(s) ? s.tune! : null
  const doppler = tune ? tune.tuned_hz - tune.nominal_hz : 0
  // "from nominal" slides the same trace by the Doppler correction
  const shift = axis === 'nominal' && tune ? doppler : 0
  const fAt = (k: number) => spec.f0_hz + k * spec.df_hz + shift
  const x0 = fAt(0)
  const x1 = fAt(n - 1)
  const dbs = spec.db10.map(v => v / 10)
  const peak = s.peak_dbfs!
  const floor = s.floor_dbfs!
  const y0 = Math.floor((floor - 7) / 10) * 10
  const y1 = Math.ceil((Math.max(peak, ...dbs) + 5) / 10) * 10
  const X = (f: number) => L + (f - x0) / (x1 - x0) * (W - L - R)
  const Y = (d: number) => T + (y1 - Math.max(y0, Math.min(y1, d))) / (y1 - y0) * (H - T - B)
  const [lo, hi] = s.edges_3db_hz!.map(f => f + shift)
  const level = peak - 3
  const receivedX = tune ? X(s.center_offset_hz! + shift) : null
  const nominalF = -doppler + shift
  const nominalX = tune && nominalF >= x0 && nominalF <= x1 ? X(nominalF) : null
  const step = (x1 - x0) > 30_000 ? 10_000 : 5_000
  const xTicks: number[] = []
  for (let f = Math.ceil(x0 / step) * step; f <= x1; f += step) xTicks.push(f)
  const yTicks: number[] = []
  for (let d = y0 + 10; d < y1; d += 10) yTicks.push(d)
  const pk = dbs.indexOf(Math.max(...dbs))
  const pts = dbs.map((d, k) => `${X(fAt(k)).toFixed(1)},${Y(d).toFixed(1)}`).join(' ')
  const mono = 'JetBrains Mono, ui-monospace, monospace'

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block w-full h-auto" role="img"
      aria-label={`Averaged beacon spectrum: peak ${peak.toFixed(1)} dBFS, floor ${floor.toFixed(1)} dBFS`}>
      {yTicks.map(d => (
        <g key={`y${d}`}>
          <line x1={L} x2={W - R} y1={Y(d)} y2={Y(d)} stroke={colors.borderSubtle} />
          <text x={L - 6} y={Y(d) + 4} fill={colors.textDisabled} fontSize={11} fontFamily={mono} textAnchor="end">{d}</text>
        </g>
      ))}
      {xTicks.map(f => (
        <g key={`x${f}`}>
          <line x1={X(f)} x2={X(f)} y1={T} y2={H - B} stroke={colors.borderSubtle} />
          <text x={X(f)} y={H - B + 16} fill={colors.textDisabled} fontSize={11} fontFamily={mono} textAnchor="middle">
            {f === 0 ? '0' : `${(f / 1000).toFixed(0)}k`}
          </text>
        </g>
      ))}
      <text x={(L + W - R) / 2} y={H - 4} fill={colors.textMuted} fontSize={11} textAnchor="middle">
        {axis === 'nominal' && tune ? `offset from nominal ${formatMhz(tune.nominal_hz).slice(0, 7)} MHz (Hz)` : 'offset from tuned center (Hz)'}
      </text>
      <text x={12} y={(T + H - B) / 2} fill={colors.textMuted} fontSize={11} textAnchor="middle"
        transform={`rotate(-90 12 ${(T + H - B) / 2})`}>dBFS / bin</text>
      <rect x={X(lo)} y={T} width={Math.max(1, X(hi) - X(lo))} height={H - B - T} fill={colors.warning} fillOpacity={0.06} />
      {bwLevels(s).map(([db, dash, lv]) => {
        if (lv.status !== 'ok' || !lv.edges_hz) return null
        const [a, z] = lv.edges_hz.map(f => X(f + shift))
        const y = Y(peak - Number(db))
        return (
          <g key={`bw${db}`}>
            <line x1={a} x2={z} y1={y} y2={y} stroke={colors.textSecondary} strokeWidth={1.2} strokeDasharray={dash || undefined} />
            {[a, z].map((x, i) => <line key={i} x1={x} x2={x} y1={y - 4} y2={y + 4} stroke={colors.textSecondary} strokeWidth={1.2} />)}
            <text x={z + 6} y={y + 4} fill={colors.textSecondary} fontSize={11} fontFamily={mono}>−{db} dB</text>
          </g>
        )
      })}
      <polyline points={pts} fill="none" stroke={colors.active} strokeWidth={1.5} strokeLinejoin="round" />
      <line x1={L} x2={W - R} y1={Y(floor)} y2={Y(floor)} stroke={colors.neutral} strokeDasharray="5 4" />
      <line x1={X(lo)} x2={X(hi)} y1={Y(level)} y2={Y(level)} stroke={colors.warning} strokeWidth={1.5} />
      {[lo, hi].map((f, i) => (
        <line key={i} x1={X(f)} x2={X(f)} y1={Y(level)} y2={H - B} stroke={colors.warning} strokeDasharray="2 3" />
      ))}
      {receivedX !== null && (
        <line x1={receivedX} x2={receivedX} y1={T} y2={H - B} stroke={colors.textPrimary} strokeDasharray="1 3" strokeWidth={1.2} />
      )}
      {nominalX !== null && (
        <g>
          <line x1={nominalX} x2={nominalX} y1={T} y2={H - B} stroke={colors.info} strokeDasharray="5 4" strokeWidth={1.5} />
          <text x={nominalX + (nominalX < W / 2 ? 6 : -6)} y={T + 14} fill={colors.info} fontSize={11} fontFamily={mono}
            textAnchor={nominalX < W / 2 ? 'start' : 'end'}>nominal</text>
        </g>
      )}
      <circle cx={X(fAt(pk))} cy={Y(dbs[pk])} r={4} fill={colors.success} stroke={colors.bgApp} strokeWidth={1.5} />
      <text x={X(fAt(pk)) + 8} y={Y(dbs[pk]) + 4} fill={colors.textPrimary} fontSize={11} fontFamily={mono}>{peak.toFixed(1)}</text>
      <text x={W - R - 4} y={Y(floor) - 6} fill={colors.textMuted} fontSize={11} fontFamily={mono} textAnchor="end">
        floor {floor.toFixed(1)}
      </text>
    </svg>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt style={{ color: colors.textMuted }}>{label}</dt>
      <dd className="m-0 font-mono tabular-nums text-right" style={{ color: colors.textPrimary }}>{children}</dd>
    </>
  )
}

function SubRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="pl-3.5" style={{ color: colors.textMuted }}>
        <span className="font-mono" style={{ color: colors.textDisabled }}>└ </span>{label}
      </dt>
      <dd className="m-0 font-mono tabular-nums text-right" style={{ color: colors.textPrimary }}>{children}</dd>
    </>
  )
}

const Sep = () => <div className="col-span-2 my-1 border-t" style={{ borderColor: colors.borderSubtle }} />

function Tag({ tone, children, title }: { tone: 'success' | 'warning' | 'danger' | 'info'; children: React.ReactNode; title?: string }) {
  const c = colors[tone]
  return (
    <span title={title} className="font-mono text-[11px] font-medium px-1.5 rounded-sm border whitespace-nowrap"
      style={{ color: c, borderColor: `${c}73` }}>
      {children}
    </span>
  )
}

function BandwidthTable({ s }: { s: SignalMeasurement }) {
  const cell = 'pt-1 font-mono tabular-nums text-right'
  const level = (label: string, edges: React.ReactNode, width: React.ReactNode) => (
    <tr key={label}>
      <td className="pt-1" style={{ color: colors.textMuted }}>{label}</td>
      <td className={cell}>{edges}</td>
      <td className={cell}>{width}</td>
    </tr>
  )
  return (
    <table className="col-span-2 w-full border-collapse" style={{ color: colors.textPrimary }}>
      <thead>
        <tr className="border-b" style={{ borderColor: colors.borderSubtle }}>
          {['Level', 'Edges', 'Width'].map((h, i) => (
            <th key={h} className={`pb-1 text-[11px] font-normal ${i ? 'text-right' : 'text-left'}`}
              style={{ color: colors.textDisabled }}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {level('−3 dB', `${signed(s.edges_3db_hz![0])} … ${signed(s.edges_3db_hz![1])} Hz`, `${s.bw_3db_hz} Hz`)}
        {bwLevels(s).map(([db, , lv]) => lv.status === 'ok' && lv.edges_hz
          ? level(`−${db} dB`, `${signed(lv.edges_hz[0])} … ${signed(lv.edges_hz[1])} Hz`, `${lv.bw_hz} Hz`)
          : level(`−${db} dB`,
              <span style={{ color: colors.textMuted }}
                title={`The −${db} dB line sits ${lv.line_above_floor_db.toFixed(1)} dB above the noise floor, too close to measure`}>
                — near floor
              </span>,
              <span style={{ color: colors.textMuted }}>—</span>))}
      </tbody>
    </table>
  )
}

function Values({ s, requiredEbn0Db }: { s: SignalMeasurement; requiredEbn0Db?: number }) {
  const hold = s.maxhold
  const showMargin = !!requiredEbn0Db && requiredEbn0Db > 0 && typeof s.ebn0_db === 'number'
  const tone = showMargin ? marginTone(s.ebn0_db!, requiredEbn0Db!) : null
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3.5 gap-y-1.5 p-3.5 content-start text-xs m-0">
      <Row label="Peak">{s.peak_dbfs!.toFixed(1)} dBFS</Row>
      <Row label="Noise floor">{s.floor_dbfs!.toFixed(1)} dBFS</Row>
      <Row label="Peak SNR">{s.snr_peak_db!.toFixed(1)} dB</Row>
      {typeof s.snr_3db_db === 'number' && <Row label="−3 dB SNR">{s.snr_3db_db.toFixed(1)} dB</Row>}
      <Sep />
      <BandwidthTable s={s} />
      <Row label="Strongest tone">{s.tone_3db_hz} Hz</Row>
      {hasNominalOffset(s) ? (
        <>
          <Sep />
          <Row label="Nominal">{formatMhz(s.tune!.nominal_hz)} MHz</Row>
          <Row label="Tuned">
            <span className="inline-flex items-center justify-end gap-1.5 flex-wrap">
              {s.tune!.doppler && <Tag tone="info" title="Doppler correction was engaged during this beacon">DOPPLER</Tag>}
              {formatMhz(s.tune!.tuned_hz)} MHz
            </span>
          </Row>
          <Row label="Received center">{formatMhz(s.received_center_hz!)} MHz</Row>
          <Row label="Offset from nominal"><span className="font-semibold">{signed(s.nominal_offset_hz!)} Hz</span></Row>
          <SubRow label="Doppler correction">
            {s.tune!.doppler ? `${signed(s.tune!.tuned_hz - s.tune!.nominal_hz)} Hz` : 'off'}
          </SubRow>
          <SubRow label="Residual (vs tuned)">{signed(s.center_offset_hz!)} Hz</SubRow>
        </>
      ) : (
        <Row label="Residual (vs tuned)">{signed(s.center_offset_hz!)} Hz</Row>
      )}
      {hold && (
        <>
          <Sep />
          <Row label="Max-hold peak">{hold.peak_dbfs.toFixed(1)} dBFS</Row>
          <Row label="Max-hold floor">{hold.floor_dbfs.toFixed(1)} dBFS</Row>
          <Row label="Max-hold SNR">{hold.snr_peak_db.toFixed(1)} dB</Row>
          {typeof hold.snr_3db_db === 'number' && <Row label="Max-hold −3 dB SNR">{hold.snr_3db_db.toFixed(1)} dB</Row>}
        </>
      )}
      <Sep />
      <Row label="C/N0">{typeof s.cn0_dbhz === 'number' ? `${s.cn0_dbhz.toFixed(1)} dB-Hz` : '--'}</Row>
      <Row label="Eb/N0">{typeof s.ebn0_db === 'number' ? `${s.ebn0_db.toFixed(1)} dB` : '--'}</Row>
      {showMargin && tone && (
        <Row label={`Margin vs ${requiredEbn0Db}`}>
          <span className="inline-flex items-center justify-end gap-1.5 flex-wrap">
            <Tag tone={tone}>{MARGIN_LABEL[tone]}</Tag>
            {signed(s.ebn0_db! - requiredEbn0Db!, 1)} dB
          </span>
        </Row>
      )}
      <Sep />
      <Row label="RBW / ENBW">{s.rbw_hz!.toFixed(1)} / {s.enbw_hz!.toFixed(1)} Hz</Row>
      <Row label="Averaged over">{Math.round(s.burst_s! * 1000)} ms burst</Row>
    </dl>
  )
}

const STRIP_W = 900
const STRIP_SIDE = 30
const STRIP_AXIS_Y = 46

/** Nominal (○), tuned (■) and received (◆) on one frequency scale: the
 *  blue run is the Doppler correction, the light run the residual. The
 *  scale stays ±12 kHz unless a value falls outside it, so strips from
 *  different beacons compare by eye. */
function LandedStrip({ s }: { s: SignalMeasurement }) {
  const tune = s.tune!
  const doppler = tune.tuned_hz - tune.nominal_hz
  const offset = s.nominal_offset_hz!
  const span = Math.max(12_000, Math.ceil(Math.max(Math.abs(doppler), Math.abs(offset)) / 4_000) * 4_000)
  const X = (hz: number) => STRIP_SIDE + (hz + span) / (2 * span) * (STRIP_W - 2 * STRIP_SIDE)
  const ticks: number[] = []
  for (let k = -span; k <= span; k += span / 3) ticks.push(k)
  const mono = 'JetBrains Mono, ui-monospace, monospace'
  const y = STRIP_AXIS_Y
  const xn = X(0)
  const xt = X(doppler)
  const xr = X(offset)
  const leftward = doppler < 0
  const toward = leftward ? 'start' : 'end'
  const kLabel = (hz: number) => hz === 0 ? '0' : `${signed(hz / 1000)}k`
  return (
    <svg viewBox={`0 0 ${STRIP_W} 96`} className="block w-full h-auto" role="img"
      aria-label={`Nominal, tuned ${signed(doppler)} Hz and received ${signed(offset)} Hz on one scale`}>
      <line x1={STRIP_SIDE} x2={STRIP_W - STRIP_SIDE} y1={y} y2={y} stroke={colors.borderStrong} />
      {ticks.map(k => (
        <g key={k}>
          <line x1={X(k)} x2={X(k)} y1={y - 4} y2={y + 4} stroke={colors.borderStrong} />
          <text x={X(k)} y={y + 20} fill={colors.textDisabled} fontSize={11} fontFamily={mono} textAnchor="middle">{kLabel(k)}</text>
        </g>
      ))}
      <text x={STRIP_W - STRIP_SIDE} y={y + 36} fill={colors.textMuted} fontSize={11} textAnchor="end">
        Hz from nominal {formatMhz(tune.nominal_hz).slice(0, 7)} MHz
      </text>
      <line x1={xn} x2={xt} y1={y} y2={y} stroke={colors.info} strokeWidth={3} strokeOpacity={0.55} />
      <line x1={xt} x2={xr} y1={y} y2={y} stroke={colors.textPrimary} strokeWidth={3} />
      <circle cx={xn} cy={y} r={5} fill={colors.bgApp} stroke={colors.info} strokeWidth={2} />
      <rect x={xt - 4} y={y - 4} width={8} height={8} fill={colors.info} />
      <path d={`M${xr} ${y - 6} L${xr + 6} ${y} L${xr} ${y + 6} L${xr - 6} ${y} Z`} fill={colors.textPrimary} />
      <text x={xn + (leftward ? 6 : -6)} y={y - 14} fill={colors.info} fontSize={11} fontFamily={mono} textAnchor={toward}>○ nominal</text>
      {tune.doppler && (
        <text x={xt + (leftward ? 4 : -4)} y={y - 30} fill={colors.info} fontSize={11} fontFamily={mono} textAnchor={toward}>
          ■ tuned {signed(doppler)}
        </text>
      )}
      <text x={xr + (leftward ? -8 : 8)} y={y - 14} fill={colors.textPrimary} fontSize={11} fontFamily={mono}
        textAnchor={leftward ? 'end' : 'start'}>◆ received {signed(offset)}</text>
    </svg>
  )
}

function AxisToggle({ axis, onChange }: { axis: SpectrumAxis; onChange: (axis: SpectrumAxis) => void }) {
  const options: [SpectrumAxis, string][] = [['tuned', 'from tuned'], ['nominal', 'from nominal']]
  return (
    <span className="inline-flex rounded-sm border overflow-hidden" role="group" aria-label="Spectrum x-axis"
      style={{ borderColor: colors.borderStrong }}>
      {options.map(([value, label]) => (
        <button key={value} type="button" aria-pressed={axis === value} onClick={() => onChange(value)}
          className="font-mono text-[11px] px-2 py-0.5 cursor-pointer border-r last:border-r-0"
          style={{
            borderColor: colors.borderStrong,
            color: axis === value ? colors.textPrimary : colors.textMuted,
            backgroundColor: axis === value ? colors.borderSubtle : 'transparent',
            boxShadow: axis === value ? `inset 0 -2px 0 ${colors.active}` : undefined,
          }}>
          {label}
        </button>
      ))}
    </span>
  )
}

export function SignalCard({ signal, requiredEbn0Db }: SignalCardProps) {
  const measured = isMeasured(signal)
  const note = measured ? floorNote(signal) : null
  const located = measured && hasNominalOffset(signal)
  const [axis, setAxis] = useState<SpectrumAxis>('tuned')

  return (
    <div className="@container rounded border overflow-hidden" style={{ borderColor: colors.borderStrong, backgroundColor: colors.bgPanel }}>
      <div className="flex items-center gap-3 flex-wrap px-3 py-2 border-b"
        style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanelRaised }}>
        <span className="flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-[0.06em]" style={{ color: colors.textMuted }}>Signal</span>
          {note && <Tag tone="warning" title={note.detail}>{note.label}</Tag>}
        </span>
        {located && <span className="ml-auto"><AxisToggle axis={axis} onChange={setAxis} /></span>}
      </div>
      {measured ? (
        <div className="grid grid-cols-1 @[640px]:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
          <div className="min-w-0 pt-2.5 pr-1.5 pb-1.5 border-b @[640px]:border-b-0 @[640px]:border-r"
            style={{ borderColor: colors.borderSubtle }}>
            <Spectrum s={signal} axis={axis} />
            <div className="flex flex-wrap gap-x-3.5 gap-y-1.5 px-3.5 pt-1.5 pb-2.5 text-[11.5px]" style={{ color: colors.textMuted }}>
              <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t-2" style={{ borderColor: colors.active }} />Spectrum (burst average)</span>
              <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t-2 border-dashed" style={{ borderColor: colors.neutral }} />Noise floor</span>
              <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t-2" style={{ borderColor: colors.warning }} />−3 dB level and edges</span>
              {signal.bw_levels && (
                <>
                  <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t" style={{ borderColor: colors.textSecondary }} />−10 dB level and edges</span>
                  <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t border-dashed" style={{ borderColor: colors.textSecondary }} />−20 dB level and edges</span>
                </>
              )}
              {located && (
                <>
                  <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t-2 border-dashed" style={{ borderColor: colors.info }} />Nominal</span>
                  <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t-2 border-dotted" style={{ borderColor: colors.textPrimary }} />Received center</span>
                </>
              )}
            </div>
          </div>
          <Values s={signal} requiredEbn0Db={requiredEbn0Db} />
          {located && (
            <div className="@[640px]:col-span-2 grid gap-2 p-3.5 border-t" style={{ borderColor: colors.borderSubtle }}>
              <span className="text-xs font-semibold uppercase tracking-[0.06em]" style={{ color: colors.textMuted }}>Where this beacon landed</span>
              <LandedStrip s={signal} />
            </div>
          )}
        </div>
      ) : (
        <p className="m-0 px-3.5 py-3 text-xs" style={{ color: colors.textMuted }}>
          Not measured: this frame arrived while the station was transmitting, so the receiver could not see a clean beacon.
        </p>
      )}
    </div>
  )
}
