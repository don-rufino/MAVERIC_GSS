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

  it('keeps older logs readable: residual only, no toggle or strip', () => {
    render(<SignalCard signal={first} />)
    expect(screen.getByText('Residual (vs tuned)')).toBeTruthy()
    expect(screen.queryByText('Offset from nominal')).toBeNull()
    expect(screen.queryByText('Where this beacon landed')).toBeNull()
  })
})
