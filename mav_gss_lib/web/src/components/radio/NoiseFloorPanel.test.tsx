import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { NoiseFloorPanel } from './NoiseFloorPanel'
import fixture from './__fixtures__/noise_floor_pass.json'
import type { NoiseFloorPass } from '@/lib/noiseFloor'

const complete = fixture.complete as unknown as NoiseFloorPass
const liveAdjusting = fixture.live_adjusting as unknown as NoiseFloorPass
const older = fixture.older as unknown as NoiseFloorPass
const { series: _s, ...completeSummary } = complete

describe('NoiseFloorPanel', () => {
  it('shows the live pass while the gain slider is moving and the antenna is held', () => {
    const { container } = render(<NoiseFloorPanel pass={liveAdjusting} live recent={[older]} />)
    expect(screen.getByText('GAIN ADJUSTING · 33 dB')).toBeTruthy()
    expect(screen.getByText('EL HELD 55°')).toBeTruthy()
    expect(screen.getByText('Quiet floor @ 27 dB')).toBeTruthy()        // 33 dB has not held 5 s yet
    expect(screen.getByText(/\(live\)/)).toBeTruthy()
    const labels = [...container.querySelectorAll('svg text')].map(t => t.textContent)
    expect(labels).toContain('RX 27 dB')
    expect(labels).not.toContain('RX 33 dB')
  })

  it('between passes shows the last pass, its gains and the recent table', () => {
    const { container } = render(
      <NoiseFloorPanel pass={complete} live={false} recent={[completeSummary as NoiseFloorPass, older]}
        nextAosMs={Date.UTC(2026, 9, 16, 20, 17)} />,
    )
    expect(screen.getByText('IDLE · next AOS 20:17Z')).toBeTruthy()
    expect(screen.getByText(/last pass · MAVERIC/)).toBeTruthy()
    expect(screen.getByText('Quiet floor @ 33 dB')).toBeTruthy()
    expect(screen.getAllByText('27 → 33 dB').length).toBeGreaterThan(0)
    const labels = [...container.querySelectorAll('svg text')].map(t => t.textContent)
    expect(labels).toEqual(expect.arrayContaining(['RX 27 dB', 'RX 33 dB', 'quiet -101.0', 'quiet -95.8']))
    expect(labels.some(t => t?.startsWith('▲ sat above 55°'))).toBe(true)
    const rows = container.querySelectorAll('tbody tr')
    expect(rows).toHaveLength(4)                                         // two gains per pass
    expect(rows[0].textContent).toContain('☀ day')
    expect(rows[0].textContent).toContain('▲ held 55°')
    expect(rows[1].textContent).toContain('33 dB')
    expect(rows[1].textContent).not.toContain('day')                     // pass columns only on its first row
    expect(rows[2].textContent).toContain('☾ night')
    expect(rows[2].textContent).not.toContain('held')
    expect(screen.queryByText('INCOMPLETE')).toBeNull()
  })

  it('flags a pass the flowgraph stopped before LOS', () => {
    render(<NoiseFloorPanel pass={{ ...complete, complete: false, end_el_deg: 18.2 }} live={false} recent={[]} />)
    expect(screen.getByText('INCOMPLETE')).toBeTruthy()
    expect(screen.getByText(/ended early · at 18°/)).toBeTruthy()
  })

  it('names the radio, shows coverage and filters recent passes by radio', () => {
    const a = { ...complete, aos_ms: complete.aos_ms, radio: { serial: 'TESTSN1', product: 'B210', name: '' },
      start_el_deg: 31.1, end_el_deg: 5.0 }
    const b = { ...complete, aos_ms: complete.aos_ms - 86_400_000, radio: { serial: 'TESTSN2', product: 'B210', name: 'SDR-B' },
      start_el_deg: 5.0, end_el_deg: 5.0 }
    const old = { ...complete, aos_ms: complete.aos_ms - 2 * 86_400_000, radio: null, start_el_deg: 5.0, end_el_deg: 5.0 }
    render(<NoiseFloorPanel pass={a} live={false} recent={[a, b, old]} radioNames={{ TESTSN1: 'SDR-A' }}
      usrp={{ serial: 'TESTSN1', product: 'B210', name: 'SDR-A' }} usrpRunning />)
    expect(screen.getAllByText(/· SDR-A/).length).toBeGreaterThan(0)            // header
    const table = screen.getByRole('table')
    expect(within(table).getAllByText('late start · from 31°').length).toBe(1)
    expect(within(table).getByText('SDR-B')).toBeTruthy()
    expect(within(table).getByText('—')).toBeTruthy()                          // recorded before serials
    fireEvent.click(screen.getByRole('button', { name: 'SDR-A (attached)' }))
    expect(within(table).queryByText('SDR-B')).toBeNull()
    expect(screen.getByText(/2 passes from other radios or with no serial hidden/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'All radios' }))
    expect(within(table).getByText('SDR-B')).toBeTruthy()
  })

  it('explains what will appear before any pass is recorded', () => {
    render(<NoiseFloorPanel pass={null} live={false} recent={[]} nextAosMs={Date.UTC(2026, 9, 16, 20, 17)} />)
    expect(screen.getByText(/No pass recorded yet/)).toBeTruthy()
    expect(screen.getByText(/next AOS 20:17Z/, { selector: 'p' })).toBeTruthy()
  })
})
