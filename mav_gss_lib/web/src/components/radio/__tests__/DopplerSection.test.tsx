import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { DopplerSection } from '../DopplerSection'

const baseProps = {
  doppler: {
    ts_ms: 1700000000000,
    station_id: 'usc',
    satellite: 'MAVERIC',
    mode: 'disconnected' as const,
    range_rate_mps: -1234.5,
    rx_hz: 437_575_000,
    rx_shift_hz: -200,
    rx_tune_hz: 437_574_800,
    tx_hz: 437_575_000,
    tx_shift_hz: 210,
    tx_tune_hz: 437_575_210,
    rx_signal_loss_db: 145.28,
    tx_signal_loss_db: 145.28,
    tx_offset_step_hz: 0,
  },
  mode: 'disconnected' as const,
  error: '',
  busy: null,
  actionError: null,
  engage: vi.fn(async () => {}),
  disengage: vi.fn(async () => {}),
  toggleStatic: vi.fn(async () => {}),
  toggleOffsetSweep: vi.fn(async () => {}),
  dismissError: vi.fn(),
}

describe('DopplerSection', () => {
  it('renders disengaged state with engage button', () => {
    render(<DopplerSection {...baseProps} />)
    expect(screen.getByText(/DISENGAGED/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Engage' })).toBeTruthy()
  })

  it('renders engaged state with disengage button and tune values', () => {
    render(<DopplerSection {...baseProps} mode="connected" doppler={{ ...baseProps.doppler, mode: 'connected' }} />)
    expect(screen.getByText('ENGAGED')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Disengage' })).toBeTruthy()
    expect(screen.getByText(/437,574,800/)).toBeTruthy()
  })

  it('shows a single collapsed Signal Loss when RX and TX frequencies match', () => {
    render(<DopplerSection {...baseProps} />)
    expect(screen.getByText('Signal Loss')).toBeTruthy()
    expect(screen.getByText('145.3 dB')).toBeTruthy()
    expect(screen.queryByText('RX Signal Loss')).toBeNull()
  })

  it('shows a reference Signal Loss @ 100 MHz alongside the real one', () => {
    render(<DopplerSection {...baseProps} />)
    expect(screen.getByText('Signal Loss @ 100 MHz')).toBeTruthy()
    expect(screen.getByText('132.5 dB')).toBeTruthy()
  })

  it('splits RX/TX Signal Loss when RX and TX frequencies differ', () => {
    const doppler = {
      ...baseProps.doppler,
      tx_hz: 437_576_000,
      rx_signal_loss_db: 145.28,
      tx_signal_loss_db: 146.05,
    }
    render(<DopplerSection {...baseProps} doppler={doppler} />)
    expect(screen.getByText('RX Signal Loss')).toBeTruthy()
    expect(screen.getByText('TX Signal Loss')).toBeTruthy()
    expect(screen.getByText('145.3 dB')).toBeTruthy()
    expect(screen.getByText('146.1 dB')).toBeTruthy()
  })

  it('calls engage on button click', () => {
    const engage = vi.fn(async () => {})
    render(<DopplerSection {...baseProps} engage={engage} />)
    fireEvent.click(screen.getByRole('button', { name: 'Engage' }))
    expect(engage).toHaveBeenCalled()
  })

  it('shows error footer when error is present', () => {
    render(<DopplerSection {...baseProps} error="invalid TLE: SGP4 error 6" />)
    expect(screen.getByText(/invalid TLE/)).toBeTruthy()
  })

  it('locks the Static Mode switch while engaged', () => {
    render(<DopplerSection {...baseProps} mode="connected" doppler={{ ...baseProps.doppler, mode: 'connected' }} />)
    const toggle = screen.getByRole('switch', { name: 'Static Mode' })
    expect(toggle.getAttribute('aria-disabled')).toBe('true')
  })

  it('locks the Engage button while Static Mode is on', () => {
    render(<DopplerSection {...baseProps} mode="static" doppler={{ ...baseProps.doppler, mode: 'static' }} />)
    expect(screen.getAllByText('STATIC').length).toBeGreaterThan(0)
    const engageBtn = screen.getByRole('button', { name: 'Engage' }) as HTMLButtonElement
    expect(engageBtn.disabled).toBe(true)
  })

  it('blanks Doppler-derived cells and shows the parked frequency in Static Mode', () => {
    render(<DopplerSection {...baseProps} mode="static" doppler={{ ...baseProps.doppler, mode: 'static' }} />)
    expect(screen.getByText('RX Parked At')).toBeTruthy()
    expect(screen.getByText('TX Parked At')).toBeTruthy()
    expect(screen.getAllByText('437,575,000 Hz')).toHaveLength(2)
    expect(screen.queryByText('RX Shift')).toBeNull()
    expect(screen.queryByText('RX Tune')).toBeNull()
  })

  it('calls toggleStatic on switch click', () => {
    const toggleStatic = vi.fn(async () => {})
    render(<DopplerSection {...baseProps} toggleStatic={toggleStatic} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Static Mode' }))
    expect(toggleStatic).toHaveBeenCalled()
  })

  it('hides Offset Sweep entirely when not available for this mission', () => {
    render(<DopplerSection {...baseProps} offsetSweepAvailable={false} />)
    expect(screen.queryByText('Offset Sweep')).toBeNull()
  })

  it('shows Offset Sweep when available, independent of Doppler mode', () => {
    render(<DopplerSection {...baseProps} mode="connected" offsetSweepAvailable />)
    expect(screen.getByText('Offset Sweep')).toBeTruthy()
    const toggle = screen.getByRole('switch', { name: 'Offset Sweep' })
    expect(toggle.getAttribute('aria-disabled')).not.toBe('true')
  })

  it('calls toggleOffsetSweep on switch click', () => {
    const toggleOffsetSweep = vi.fn(async () => {})
    render(<DopplerSection {...baseProps} offsetSweepAvailable toggleOffsetSweep={toggleOffsetSweep} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Offset Sweep' }))
    expect(toggleOffsetSweep).toHaveBeenCalled()
  })

  it('shows the live TX Offset only when the sweep is enabled', () => {
    const { rerender } = render(<DopplerSection {...baseProps} offsetSweepAvailable offsetSweepEnabled={false} />)
    expect(screen.queryByText('TX Offset')).toBeNull()

    rerender(<DopplerSection {...baseProps} offsetSweepAvailable offsetSweepEnabled
      doppler={{ ...baseProps.doppler, tx_offset_step_hz: 1200 }} />)
    expect(screen.getByText('TX Offset')).toBeTruthy()
    expect(screen.getByText('+1200 Hz')).toBeTruthy()
  })
})
