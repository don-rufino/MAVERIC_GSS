import { describe, expect, it } from 'vitest'
import { buildRxRow, buildTxRow } from '@/lib/columns'
import type { ColumnDef, RxPacket, TxQueueCmd } from '@/lib/types'

const cmdColumn: ColumnDef = { id: 'cmd', label: 'cmd', kind: 'cmd_id' }

function rxPacket(cmdId: string): RxPacket {
  return {
    num: 1, frame: 'ASM+GOLAY', size: 4, raw_hex: 'deadbeef',
    received_at_ms: 0, transport_meta: {}, warnings: [],
    is_echo: false, is_dup: false, is_unknown: false,
    integrity_ok: true,
    mission: { id: 'maveric', cmd_id: cmdId, facts: { header: {} } },
  } as RxPacket
}

function txItem(cmdId: string): TxQueueCmd {
  return {
    type: 'mission_cmd', num: 1, cmd_id: cmdId,
    mission: { id: 'maveric', cmd_id: cmdId, facts: { header: {} } },
    parameters: [],
  } as TxQueueCmd
}

describe('buildRxRow / buildTxRow with kind: cmd_id', () => {
  it('resolves cmd column from envelope mission.cmd_id for RX', () => {
    const row = buildRxRow(rxPacket('eps_hk'), [cmdColumn])
    expect(row.cmd.value).toBe('eps_hk')
  })

  it('resolves cmd column from envelope mission.cmd_id for TX', () => {
    const row = buildTxRow(txItem('com_ping'), [cmdColumn])
    expect(row.cmd.value).toBe('com_ping')
  })

  it('renders -- when cmd_id is absent at the envelope level', () => {
    const pkt = rxPacket('')
    delete (pkt.mission as { cmd_id?: string }).cmd_id
    const row = buildRxRow(pkt, [cmdColumn])
    expect(row.cmd.value).toBe('--')
  })
})
