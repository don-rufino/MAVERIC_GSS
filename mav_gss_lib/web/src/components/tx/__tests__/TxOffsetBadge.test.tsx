import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TxOffsetBadge } from '../TxOffsetBadge'

describe('TxOffsetBadge', () => {
  it('shows -- for a row with no known offset yet (not sent)', () => {
    render(<TxOffsetBadge offsetHz={undefined} />)
    expect(screen.getByText('--')).toBeTruthy()
  })

  it('shows 0.0 kHz, not --, for a real zero offset', () => {
    render(<TxOffsetBadge offsetHz={0} />)
    expect(screen.getByText('0.0 kHz')).toBeTruthy()
  })

  it('formats a positive offset with an explicit sign', () => {
    render(<TxOffsetBadge offsetHz={1200} />)
    expect(screen.getByText('+1.2 kHz')).toBeTruthy()
  })

  it('formats a negative offset', () => {
    render(<TxOffsetBadge offsetHz={-2400} />)
    expect(screen.getByText('-2.4 kHz')).toBeTruthy()
  })
})
