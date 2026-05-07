import type { CommandInstance, VerifierSpec, VerifierOutcome } from '@/lib/types'
import { colors } from '@/lib/colors'

interface VerifierTickStripProps {
  instance: CommandInstance | null
  now_ms: number
}

type DotState = 'pending' | 'passed' | 'failed' | 'window_expired'

interface Dot {
  key: string
  label: string
  state: DotState
  pulse: boolean
}

function dotColor(state: DotState): string {
  if (state === 'passed') return colors.success
  if (state === 'failed') return colors.danger
  // Window expired = silent timeout — treat as a failure at the dot level,
  // consistent with the left-rail aggregate (any non-success outcome → red).
  // NACK-vs-expired distinction is still visible via the `!` badge: NACK
  // fires `!`, expired does not.
  if (state === 'window_expired') return colors.danger
  return 'transparent'
}

function outcomeOf(inst: CommandInstance, vid: string): VerifierOutcome {
  return inst.outcomes[vid] ?? { state: 'pending', matched_at_ms: null, match_event_id: null }
}

function nearExpiry(spec: VerifierSpec, now_ms: number, t0_ms: number): boolean {
  return (now_ms - t0_ms) / spec.check_window.stop_ms > 0.8
}

/**
 * Collapse multiple received-stage ACK verifiers (e.g. ack_a + ack_b)
 * into a single aggregate dot. Operationally, "did we get ACK coverage?"
 * is one signal; which specific link is in the detail block.
 * Aggregation rule:
 *
 *   - all passed          → passed
 *   - all window_expired  → window_expired
 *   - any passed          → passed (command reached at least one hop)
 *   - any pending         → pending (still waiting on a hop)
 *   - else                → window_expired
 *
 * Pulse fires if any pending verifier is past 80% of its window.
 */
function aggregateReceived(inst: CommandInstance, specs: VerifierSpec[], now_ms: number): Dot {
  const outcomes = specs.map(s => outcomeOf(inst, s.verifier_id))
  const allPassed = outcomes.every(o => o.state === 'passed')
  const anyPassed = outcomes.some(o => o.state === 'passed')
  const anyPending = outcomes.some(o => o.state === 'pending')
  const allExpired = outcomes.every(o => o.state === 'window_expired')
  const pulse = specs.some((s, i) => outcomes[i].state === 'pending' && nearExpiry(s, now_ms, inst.t0_ms))
  let state: DotState
  if (allPassed) state = 'passed'
  else if (anyPassed) state = 'passed'
  else if (anyPending) state = 'pending'
  else if (allExpired) state = 'window_expired'
  else state = 'pending'
  return { key: 'ack', label: `ACK · ${specs.map(s => s.display_label).join('+')}`, state, pulse }
}

function buildDots(instance: CommandInstance, now_ms: number): Dot[] {
  const out: Dot[] = []
  const received = instance.verifier_set.verifiers.filter(v => v.stage === 'received')
  const complete = instance.verifier_set.verifiers.filter(v => v.stage === 'complete')
  const failed = instance.verifier_set.verifiers.filter(v => v.stage === 'failed')

  if (received.length === 1) {
    const v = received[0]
    const o = outcomeOf(instance, v.verifier_id)
    out.push({
      key: v.verifier_id,
      label: `${v.display_label} · ${o.state}`,
      state: (o.state as DotState),
      pulse: o.state === 'pending' && nearExpiry(v, now_ms, instance.t0_ms),
    })
  } else if (received.length > 1) {
    out.push(aggregateReceived(instance, received, now_ms))
  }

  for (const v of complete) {
    const o = outcomeOf(instance, v.verifier_id)
    out.push({
      key: v.verifier_id,
      label: `${v.display_label} · ${o.state}`,
      state: (o.state as DotState),
      pulse: o.state === 'pending' && nearExpiry(v, now_ms, instance.t0_ms),
    })
  }

  // NACK only shown when actually fired (any failed-stage verifier passed).
  const firedNack = failed.find(v => outcomeOf(instance, v.verifier_id).state === 'passed')
  if (firedNack) {
    out.push({ key: firedNack.verifier_id, label: `NACK · ${firedNack.display_label}`, state: 'failed', pulse: false })
  }
  return out
}

export function VerifierTickStrip({ instance, now_ms }: VerifierTickStripProps) {
  if (!instance) {
    return <span style={{ display: 'inline-block', width: 60 }} aria-hidden />
  }
  const dots = buildDots(instance, now_ms)
  const nackFired = dots.some(d => d.state === 'failed')
  return (
    <span className="inline-flex items-center gap-1" title={instance.stage.toUpperCase()}>
      {dots.map(d => {
        const filled = d.state === 'passed' || d.state === 'failed' || d.state === 'window_expired'
        return (
          <span key={d.key}
            className={`inline-block rounded-full ${d.pulse ? 'animate-pulse-warning' : ''}`}
            style={{
              width: 12, height: 12,
              backgroundColor: filled ? dotColor(d.state) : 'transparent',
              border: filled ? 'none' : `2px solid ${colors.active}`,
              boxShadow: filled ? `0 0 0 1px ${colors.borderStrong}` : undefined,
            }}
            title={d.label}
          />
        )
      })}
      {nackFired && (
        <span className="ml-0.5 text-[10px] font-bold" style={{ color: colors.danger }}>!</span>
      )}
    </span>
  )
}
