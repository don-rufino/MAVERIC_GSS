import type { CSSProperties } from 'react'
import type { ColumnDef, RenderCell, RxPacket, TxQueueCmd, TxHistoryItem } from '@/lib/types'
import { frameColor } from '@/lib/colors'
import { packetFlags, rxTime } from '@/lib/rxPacket'

// Tailwind width tokens for platform-shell UI scaffolding (RX expand
// chevron + TX queue drag/number/action gutters). Mission-authored
// columns get their widths from `mission.yml::ui.{rx,tx}_columns[].width`
// — never from this list.
export const col = {
  chevron: 'w-5',       // expand indicator
  num:     'w-6',       // queue position number (right-aligned tabular-nums; fits up to 3 digits)
  grip:    'w-[22px]',  // drag handle
  actions: 'w-[60px]',  // queue row buttons
} as const

// Platform RX shell columns. Each entry pairs a column def with a function
// that produces the cell from the packet — single source of truth per
// column. Mission-authored columns from `mission.yml::ui.rx_columns` slot
// in between PRE and POST in `composeRxColumns`.
const PLATFORM_RX_SHELL_PRE: ReadonlyArray<readonly [ColumnDef, (p: RxPacket) => RenderCell]> = [
  [{ id: 'num',   label: '#',     width: 36, align: 'right' },
    (p) => ({ value: p.num, tabular: true })],
  [{ id: 'time',  label: 'time',  width: 68 },
    (p) => ({ value: rxTime(p), monospace: true, tabular: true })],
  [{ id: 'frame', label: 'frame', width: 72, toggle: 'showFrame' },
    (p) => ({ value: p.frame || '--', monospace: true, tone: frameColor(p.frame || '') })],
]

const PLATFORM_RX_SHELL_POST: ReadonlyArray<readonly [ColumnDef, (p: RxPacket) => RenderCell]> = [
  [{ id: 'flags', label: '',     width: 72, align: 'right' },
    (p) => ({ value: packetFlags(p) })],
  [{ id: 'size',  label: 'size', width: 40, align: 'right' },
    (p) => ({ value: p.size, suffix: 'B', tabular: true })],
]

const RX_SHELL_BUILDERS = new Map<string, (p: RxPacket) => RenderCell>([
  ...PLATFORM_RX_SHELL_PRE.map(([c, b]) => [c.id, b] as [string, (p: RxPacket) => RenderCell]),
  ...PLATFORM_RX_SHELL_POST.map(([c, b]) => [c.id, b] as [string, (p: RxPacket) => RenderCell]),
])

export function composeRxColumns(missionColumns: ColumnDef[]): ColumnDef[] {
  return [
    ...PLATFORM_RX_SHELL_PRE.map(([c]) => c),
    ...missionColumns,
    ...PLATFORM_RX_SHELL_POST.map(([c]) => c),
  ]
}

// Column geometry helpers — used by CellValue, QueueItem (verifier
// branch), TxQueue (header row) and PacketList (header row) so header
// labels and cell content always share the same box geometry.
//
// Width is applied as an inline style (not a Tailwind class) so any
// pixel value works without needing a build-time class allowlist. The
// className carries only the shrink / flex / truncate behavior.
export function columnWidthClass(c: ColumnDef): string {
  if (c.flex) return 'flex-1 shrink-0'
  if (c.truncate) return 'shrink-0 truncate'
  return 'shrink-0'
}

export function columnWidthStyle(c: ColumnDef): CSSProperties | undefined {
  if (c.flex || c.width == null) return undefined
  return { width: `${c.width}px` }
}

export function columnAlignClass(c: ColumnDef): string {
  return c.align === 'right' ? 'ml-auto text-right' : ''
}

// TX columns are fully mission-declared (no platform shell). The verifiers
// tick strip is dispatched on `c.kind === 'verifiers'`.
export function buildRxRow(packet: RxPacket, columns: ColumnDef[]): Record<string, RenderCell> {
  const facts = packet.mission?.facts as Record<string, unknown> | undefined
  const cmdId = packet.mission?.cmd_id as string | undefined
  const row: Record<string, RenderCell> = {}
  for (const c of columns) {
    const shellBuild = RX_SHELL_BUILDERS.get(c.id)
    row[c.id] = shellBuild ? shellBuild(packet) : missionCell(c, facts, cmdId)
  }
  return row
}

export function buildTxRow(
  item: TxQueueCmd | TxHistoryItem, columns: ColumnDef[],
): Record<string, RenderCell> {
  const facts = item.mission?.facts as Record<string, unknown> | undefined
  const cmdId = item.mission?.cmd_id as string | undefined
  const row: Record<string, RenderCell> = {}
  for (const c of columns) {
    row[c.id] = missionCell(c, facts, cmdId)
  }
  return row
}

function missionCell(
  c: ColumnDef, facts: Record<string, unknown> | undefined,
  cmdId?: string,
): RenderCell {
  if (c.kind === 'verifiers') {
    // CellValue dispatches on `c.kind` to render the verifier tick strip
    // from the per-row CommandInstance map. No path lookup.
    return { value: null }
  }
  if (c.kind === 'cmd_id') {
    // cmd_id is canonical at the envelope level (mission.cmd_id), not
    // under mission.facts — the codec deliberately avoids duplicating it
    // under header.cmd_id. Resolve from the envelope directly.
    return {
      value: formatCellValue(cmdId),
      monospace: true,
    }
  }
  const path = c.path ?? ''
  const value = path ? resolvePath(facts, path) : undefined
  return {
    value: formatCellValue(value),
    badge: Boolean(c.badge),
    monospace: !c.badge,
  }
}

function resolvePath(root: unknown, path: string): unknown {
  if (root == null || typeof root !== 'object') return undefined
  let cur: unknown = root
  for (const part of path.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[part]
  }
  return cur
}

function formatCellValue(v: unknown): string {
  if (v == null || v === '') return '--'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
