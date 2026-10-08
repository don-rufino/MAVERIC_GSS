import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SignalCard } from './SignalCard'
import fixture from './__fixtures__/scionx_20260923_signal.json'
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

  it('explains frames received while transmitting', () => {
    render(<SignalCard signal={{ v: 1, status: 'during_tx' }} />)
    expect(screen.getByText(/while the station was transmitting/)).toBeTruthy()
  })
})
