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
  },
  mode: 'disconnected' as const,
  error: '',
  busy: null,
  actionError: null,
  engage: vi.fn(async () => {}),
  disengage: vi.fn(async () => {}),
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
})
