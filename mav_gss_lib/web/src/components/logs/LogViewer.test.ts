import { describe, expect, it } from 'vitest'
import { entryLabel } from './LogViewer'
import type { LogEntry } from '@/hooks/useLogQuery'

function rxEntry(facts: Record<string, unknown>, cmdId = ''): LogEntry {
  return {
    event_kind: 'rx_packet',
    mission: { id: 'suchai4', cmd_id: cmdId, facts },
  }
}

describe('entryLabel', () => {
  it('joins type and kind when a beacon decoded', () => {
    const e = rxEntry({ header: { type: 'BCN' }, beacon: { kind: 'hk' } })
    expect(entryLabel(e)).toBe('BCN · hk')
  })

  it('shows type alone when there is no decoded kind', () => {
    const e = rxEntry({ header: { type: 'TLM' } })
    expect(entryLabel(e)).toBe('TLM')
  })

  it('shows type alone for an unknown frame', () => {
    const e = rxEntry({ header: { type: 'UNK' } })
    expect(entryLabel(e)).toBe('UNK')
  })

  it('is a no-op for MAVERIC-shaped facts (no header.type)', () => {
    const e = rxEntry({ header: { src: 'GS', dest: 'LPPM', echo: 'NONE', ptype: 'CMD' } })
    expect(entryLabel(e)).toBe('')
  })

  it('still prioritizes cmd_id over type/kind when both are present', () => {
    const e = rxEntry({ header: { type: 'BCN' }, beacon: { kind: 'hk' } }, 'com_ping')
    expect(entryLabel(e)).toBe('com_ping')
  })

  it('resolves TX rows from cmd_id, unaffected by this change', () => {
    const e: LogEntry = {
      event_kind: 'tx_command',
      mission: { id: 'maveric', cmd_id: 'com_ping', facts: { header: { ptype: 'CMD' } } },
    }
    expect(entryLabel(e)).toBe('com_ping')
  })

  it('returns empty string with no mission facts at all', () => {
    expect(entryLabel({ event_kind: 'rx_packet' })).toBe('')
  })
})
