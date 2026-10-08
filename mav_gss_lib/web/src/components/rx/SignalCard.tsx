import { colors } from '@/lib/colors'
import { MARGIN_LABEL, floorNote, isMeasured, marginTone, signed } from '@/lib/signal'
import type { SignalMeasurement } from '@/lib/types'

interface SignalCardProps {
  /** This packet's own beacon measurement. */
  signal: SignalMeasurement
  /** Required downlink Eb/N0 from the link budget; 0/undefined hides margin. */
  requiredEbn0Db?: number
}

const W = 600
const H = 300
const L = 46
const R = 10
const T = 12
const B = 34

function Spectrum({ s }: { s: SignalMeasurement }) {
  const spec = s.spectrum!
  const n = spec.db10.length
  const fAt = (k: number) => spec.f0_hz + k * spec.df_hz
  const x0 = spec.f0_hz
  const x1 = fAt(n - 1)
  const dbs = spec.db10.map(v => v / 10)
  const peak = s.peak_dbfs!
  const floor = s.floor_dbfs!
  const y0 = Math.floor((floor - 7) / 10) * 10
  const y1 = Math.ceil((Math.max(peak, ...dbs) + 5) / 10) * 10
  const X = (f: number) => L + (f - x0) / (x1 - x0) * (W - L - R)
  const Y = (d: number) => T + (y1 - Math.max(y0, Math.min(y1, d))) / (y1 - y0) * (H - T - B)
  const [lo, hi] = s.edges_3db_hz!
  const level = peak - 3
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
      <text x={(L + W - R) / 2} y={H - 4} fill={colors.textMuted} fontSize={11} textAnchor="middle">offset from tuned center (Hz)</text>
      <text x={12} y={(T + H - B) / 2} fill={colors.textMuted} fontSize={11} textAnchor="middle"
        transform={`rotate(-90 12 ${(T + H - B) / 2})`}>dBFS / bin</text>
      <rect x={X(lo)} y={T} width={Math.max(1, X(hi) - X(lo))} height={H - B - T} fill={colors.warning} fillOpacity={0.06} />
      <polyline points={pts} fill="none" stroke={colors.active} strokeWidth={1.5} strokeLinejoin="round" />
      <line x1={L} x2={W - R} y1={Y(floor)} y2={Y(floor)} stroke={colors.neutral} strokeDasharray="5 4" />
      <line x1={X(lo)} x2={X(hi)} y1={Y(level)} y2={Y(level)} stroke={colors.warning} strokeWidth={1.5} />
      {[lo, hi].map((f, i) => (
        <line key={i} x1={X(f)} x2={X(f)} y1={Y(level)} y2={H - B} stroke={colors.warning} strokeDasharray="2 3" />
      ))}
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

const Sep = () => <div className="col-span-2 my-1 border-t" style={{ borderColor: colors.borderSubtle }} />

function Tag({ tone, children, title }: { tone: 'success' | 'warning' | 'danger'; children: React.ReactNode; title?: string }) {
  const c = colors[tone]
  return (
    <span title={title} className="font-mono text-[11px] font-medium px-1.5 rounded-sm border whitespace-nowrap"
      style={{ color: c, borderColor: `${c}73` }}>
      {children}
    </span>
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
      <Row label="−3 dB edges">{signed(s.edges_3db_hz![0])} … {signed(s.edges_3db_hz![1])} Hz</Row>
      <Row label="−3 dB bandwidth">{s.bw_3db_hz} Hz</Row>
      <Row label="Strongest tone">{s.tone_3db_hz} Hz</Row>
      <Row label="Center offset">{signed(s.center_offset_hz!)} Hz</Row>
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

export function SignalCard({ signal, requiredEbn0Db }: SignalCardProps) {
  const measured = isMeasured(signal)
  const note = measured ? floorNote(signal) : null

  return (
    <div className="@container rounded border overflow-hidden" style={{ borderColor: colors.borderStrong, backgroundColor: colors.bgPanel }}>
      <div className="flex items-center gap-3 flex-wrap px-3 py-2 border-b"
        style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanelRaised }}>
        <span className="flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-[0.06em]" style={{ color: colors.textMuted }}>Signal</span>
          {note && <Tag tone="warning" title={note.detail}>{note.label}</Tag>}
        </span>
      </div>
      {measured ? (
        <div className="grid grid-cols-1 @[640px]:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
          <div className="min-w-0 pt-2.5 pr-1.5 pb-1.5 border-b @[640px]:border-b-0 @[640px]:border-r"
            style={{ borderColor: colors.borderSubtle }}>
            <Spectrum s={signal} />
            <div className="flex flex-wrap gap-x-3.5 gap-y-1.5 px-3.5 pt-1.5 pb-2.5 text-[11.5px]" style={{ color: colors.textMuted }}>
              <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t-2" style={{ borderColor: colors.active }} />Spectrum (burst average)</span>
              <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t-2 border-dashed" style={{ borderColor: colors.neutral }} />Noise floor</span>
              <span><i className="inline-block w-3.5 align-middle mr-1.5 border-t-2" style={{ borderColor: colors.warning }} />−3 dB level and edges</span>
            </div>
          </div>
          <Values s={signal} requiredEbn0Db={requiredEbn0Db} />
        </div>
      ) : (
        <p className="m-0 px-3.5 py-3 text-xs" style={{ color: colors.textMuted }}>
          Not measured: this frame arrived while the station was transmitting, so the receiver could not see a clean beacon.
        </p>
      )}
    </div>
  )
}
