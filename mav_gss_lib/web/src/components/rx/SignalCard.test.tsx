import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SignalCard } from './SignalCard'
import fixture from './__fixtures__/scionx_20260923_signal.json'
import tuned from './__fixtures__/scionx_20260923_signal_tuned.json'
import type { SignalMeasurement } from '@/lib/types'

const [first, second] = fixture.beacons as unknown as SignalMeasurement[]

describe('SignalCard (real SCION-X beacons)', () => {
  it('shows the measured values, max hold and the spectrum plot', () => {
    const { container } = render(
      <SignalCard signal={first} requiredEbn0Db={7.8} />,
    )
    expect(screen.getByText(`${first.peak_dbfs!.toFixed(1)} dBFS`)).toBeTruthy()
    expect(screen.getByText(`${first.floor_dbfs!.toFixed(1)} dBFS`)).toBeTruthy()
    expect(screen.getByText(`${first.maxhold!.floor_dbfs.toFixed(1)} dBFS`)).toBeTruthy()
    expect(screen.getByText(`${first.bw_3db_hz} Hz`)).toBeTruthy()
    expect(screen.getByText('−3 dB SNR')).toBeTruthy()
    expect(screen.getByText(`${first.snr_3db_db!.toFixed(1)} dB`)).toBeTruthy()
    expect(screen.getByText('MARGIN OK')).toBeTruthy()
    expect(container.querySelector('svg polyline')).toBeTruthy()
    expect(screen.getByRole('img').getAttribute('aria-label')).toMatch(/peak -?\d/)
  })

  it('shows only its own beacon, with no beacon switching', () => {
    render(<SignalCard signal={second} />)
    expect(screen.queryByRole('group')).toBeNull()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.getByText(`${second.peak_dbfs!.toFixed(1)} dBFS`)).toBeTruthy()
  })

  it('hides the margin when no required Eb/N0 is configured', () => {
    render(<SignalCard signal={first} />)
    expect(screen.queryByText(/MARGIN|MARGINAL|BELOW REQUIRED/)).toBeNull()
  })

  it('tabulates the −3, −10 and −20 dB bandwidth and draws the lower levels', () => {
    const { container } = render(<SignalCard signal={first} />)
    const rows = [...container.querySelectorAll('tbody tr')].map(tr => tr.textContent)
    expect(rows).toEqual([
      '−3 dB−3027 … +2051 Hz5176 Hz',
      '−10 dB−3027 … +2148 Hz5273 Hz',
      '−20 dB−3613 … +2734 Hz6445 Hz',
    ])
    expect(screen.getByText('Strongest tone')).toBeTruthy()
    const labels = [...container.querySelectorAll('svg text')].map(t => t.textContent)
    expect(labels).toContain('−10 dB')
    expect(labels).toContain('−20 dB')
    expect(screen.getByText('−20 dB level and edges')).toBeTruthy()
  })

  it('shows a level too close to the noise floor as near floor, not a width', () => {
    // the pass's third (weakest) beacon: -20 dB line only 1.0 dB above the floor
    const weak = { ...first, bw_levels: { ...first.bw_levels, 20: { status: 'near_floor', line_above_floor_db: 1.0 } } }
    const { container } = render(<SignalCard signal={weak as SignalMeasurement} />)
    expect(screen.getByText('— near floor').getAttribute('title')).toMatch(/1\.0 dB above the noise floor/)
    expect(container.querySelector('tbody tr:last-child')!.textContent).toBe('−20 dB— near floor—')
    const labels = [...container.querySelectorAll('svg text')].map(t => t.textContent)
    expect(labels).toContain('−10 dB')
    expect(labels).not.toContain('−20 dB')
  })

  it('explains frames received while transmitting', () => {
    render(<SignalCard signal={{ v: 1, status: 'during_tx' }} />)
    expect(screen.getByText(/while the station was transmitting/)).toBeTruthy()
  })
})

const [tunedFirst] = tuned.beacons as unknown as SignalMeasurement[]

describe('SignalCard offset from nominal (real SCION-X beacon, recorded Doppler)', () => {
  it('splits the offset from nominal into Doppler correction and residual', () => {
    render(<SignalCard signal={tunedFirst} />)
    expect(screen.getByText('437.500 000 MHz')).toBeTruthy()
    expect(screen.getByText('DOPPLER')).toBeTruthy()
    expect(screen.getByText('437.507 771 MHz')).toBeTruthy()       // received center (rounded)
    expect(screen.getByText('+7771 Hz')).toBeTruthy()              // offset from nominal
    expect(screen.getByText('Doppler correction')).toBeTruthy()
    expect(screen.getByText('Residual (vs tuned)')).toBeTruthy()
    expect(screen.getByText('−488 Hz')).toBeTruthy()
    expect(screen.queryByText('Center offset')).toBeNull()
    expect(screen.getByText('Where this beacon landed')).toBeTruthy()
  })

  it('toggles the spectrum axis between tuned and nominal', () => {
    render(<SignalCard signal={tunedFirst} />)
    expect(screen.getByText('offset from tuned center (Hz)')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'from nominal' }))
    expect(screen.getByText('offset from nominal 437.500 MHz (Hz)')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'from nominal' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('keeps beacons logged before −10/−20 dB existed readable: −3 dB row only', () => {
    const { container } = render(<SignalCard signal={tunedFirst} />)
    expect(tunedFirst.bw_levels).toBeUndefined()
    expect(container.querySelectorAll('tbody tr')).toHaveLength(1)
    expect(screen.queryByText('−10 dB level and edges')).toBeNull()
  })

  it('keeps older logs readable: residual only, no toggle or strip', () => {
    render(<SignalCard signal={first} />)
    expect(screen.getByText('Residual (vs tuned)')).toBeTruthy()
    expect(screen.queryByText('Offset from nominal')).toBeNull()
    expect(screen.queryByText('Where this beacon landed')).toBeNull()
  })
})
