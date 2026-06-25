import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { RowRenderer } from './fields'
import { SearchContext } from './search'

function withSearch(query: string, ui: React.ReactNode) {
  return <SearchContext.Provider value={query}>{ui}</SearchContext.Provider>
}

describe('RowRenderer toggle', () => {
  it('renders a switch and reports changes', () => {
    const onChange = vi.fn()
    render(withSearch('', (
      <RowRenderer row={{ id: 't', label: 'Auto-refresh', control: { kind: 'toggle', value: false, onChange } }} />
    )))
    const sw = screen.getByRole('switch')
    expect(sw).toBeTruthy()
    fireEvent.click(sw)
    expect(onChange).toHaveBeenCalledWith(true)
  })
})

describe('RowRenderer search visibility', () => {
  it('hides itself when the query does not match', () => {
    render(withSearch('zzz', (
      <RowRenderer row={{ id: 'f', label: 'RX frequency', description: 'Downlink center', control: { kind: 'text', value: 'x', onChange: () => {} } }} />
    )))
    expect(screen.queryByText('RX frequency')).toBeNull()
  })
  it('shows when the query matches the description', () => {
    render(withSearch('downlink', (
      <RowRenderer row={{ id: 'f', label: 'RX frequency', description: 'Downlink center', control: { kind: 'text', value: 'x', onChange: () => {} } }} />
    )))
    expect(screen.getByText('RX frequency')).toBeTruthy()
  })
})
