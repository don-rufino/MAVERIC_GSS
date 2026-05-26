import { useMemo, useRef, useEffect, useCallback } from 'react'
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso'
import { PacketRow } from './PacketRow'
import { colors } from '@/lib/colors'
import { composeRxColumns, columnWidthClass, columnWidthStyle, columnAlignClass } from '@/lib/columns'
import type { ColumnDef, ParamUpdate, RxPacket } from '@/lib/types'

const FALLBACK_COLUMNS = composeRxColumns([])

interface PacketListProps {
  packets: RxPacket[]
  packetParametersById?: Record<string, ParamUpdate[]>
  columns?: ColumnDef[]
  showFrame: boolean
  showEcho: boolean
  flashPacketNum?: number | null
  selectedNum: number | null
  onSelect: (num: number) => void
  autoScroll: boolean
  onScrolledUp: () => void
  zmqStatus?: string
  scrollSignal?: number
  compact?: boolean
}

const MAX_DOM_PACKETS = 5000
const SCROLL_SUPPRESS_MS = 120
const BOTTOM_SCROLL_GUTTER_PX = 8
const BOTTOM_UNLOCK_THRESHOLD_PX = BOTTOM_SCROLL_GUTTER_PX + 8

export function PacketList({
  packets, packetParametersById, columns, showFrame, showEcho, flashPacketNum, selectedNum, onSelect,
  autoScroll, onScrolledUp, zmqStatus, scrollSignal, compact,
}: PacketListProps) {
  const isStale = zmqStatus ? ['DOWN', 'OFFLINE'].includes(zmqStatus.toUpperCase()) : false
  const effectiveColumns = columns && columns.length > 0 ? columns : FALLBACK_COLUMNS
  const virtuosoRef = useRef<VirtuosoHandle | null>(null)
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const suppressScroll = useRef(true)
  const suppressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const filtered = useMemo(
    () => (packets.length > MAX_DOM_PACKETS ? packets.slice(-MAX_DOM_PACKETS) : packets),
    [packets],
  )

  const scrollToBottom = useCallback(() => {
    const index = filtered.length - 1
    if (index < 0) return
    virtuosoRef.current?.scrollToIndex({
      index,
      align: 'end',
      behavior: 'auto',
    })
    requestAnimationFrame(() => {
      virtuosoRef.current?.scrollToIndex({
        index,
        align: 'end',
        behavior: 'auto',
      })
    })
  }, [filtered.length])

  // Auto-scroll to bottom when new packets arrive or container resizes
  useEffect(() => {
    if (autoScroll && filtered.length > 0) {
      suppressScroll.current = true
      if (suppressTimerRef.current) clearTimeout(suppressTimerRef.current)
      scrollToBottom()
      suppressTimerRef.current = setTimeout(() => {
        suppressScroll.current = false
        suppressTimerRef.current = null
      }, SCROLL_SUPPRESS_MS)
    }
  }, [filtered.length, autoScroll, scrollSignal, scrollToBottom])

  useEffect(() => {
    return () => {
      if (suppressTimerRef.current) clearTimeout(suppressTimerRef.current)
    }
  }, [])

  useEffect(() => {
    const el = viewportRef.current
    if (!el || !autoScroll || filtered.length === 0) return

    let frameA = 0
    let frameB = 0
    const observer = new ResizeObserver(() => {
      frameA = requestAnimationFrame(() => {
        scrollToBottom()
        frameB = requestAnimationFrame(() => {
          scrollToBottom()
        })
      })
    })

    observer.observe(el)

    return () => {
      observer.disconnect()
      if (frameA) cancelAnimationFrame(frameA)
      if (frameB) cancelAnimationFrame(frameB)
    }
  }, [autoScroll, filtered.length, scrollToBottom])

  // Detect user scrolling up to unlock — ignore programmatic scrolls
  const handleBottomStateChange = useCallback((isAtBottom: boolean) => {
    if (suppressScroll.current) return
    if (!isAtBottom) {
      onScrolledUp()
    }
  }, [onScrolledUp])

  return (
    <>
      {isStale && (
        <div className="flex items-center justify-center gap-2 px-3 py-1 text-xs font-semibold shrink-0"
          style={{ backgroundColor: colors.dangerFill, color: colors.danger, borderBottom: `1px solid ${colors.danger}40` }}>
          ⚠ DATA STALE — ZMQ disconnected
        </div>
      )}

      {filtered.length > 0 && (
        // Mirror the row wrapper geometry: 2px transparent left border
        // matches `.pkt-row-wrap`'s active/inactive border so header
        // labels start at the same x as their row cells. No right
        // padding so align=right header labels finish flush with the
        // align=right row cells.
        <div className="flex items-center text-[11px] font-light py-0.5 shrink-0 border-l-2 border-transparent" style={{ color: colors.sep }}>
          {!compact && <span className="w-5 px-1" />}
          {effectiveColumns.map(c => {
            if (c.toggle === 'showFrame' && !showFrame) return null
            if (c.toggle === 'showEcho' && !showEcho) return null
            // Same column geometry helpers CellValue uses on the data
            // rows — keeps header labels lined up over their cells.
            return (
              <span
                key={c.id}
                className={`py-1 px-1 ${columnWidthClass(c)} ${columnAlignClass(c)} whitespace-nowrap`}
                style={columnWidthStyle(c)}
              >
                {c.label}
              </span>
            )
          })}
        </div>
      )}

      {filtered.length === 0 ? (
        <div className="flex-1 flex items-center justify-center" style={{ color: colors.dim }}>
          <span className="text-xs py-8">Idle — no packets received</span>
        </div>
      ) : (
        <div ref={viewportRef} className="flex-1 min-h-0">
          <Virtuoso
            ref={virtuosoRef}
            className="h-full overflow-x-hidden"
            data={filtered}
            atBottomStateChange={handleBottomStateChange}
            atBottomThreshold={BOTTOM_UNLOCK_THRESHOLD_PX}
            overscan={300}
            components={{
              Footer: () => <div style={{ height: BOTTOM_SCROLL_GUTTER_PX }} aria-hidden="true" />,
            }}
            computeItemKey={(_, pkt) => pkt.num}
            itemContent={(_, pkt) => {
              const isActive = !compact && selectedNum === pkt.num
              const wrapClasses = compact
                ? `${pkt.num === flashPacketNum ? 'pkt-flash' : ''}`
                : `pkt-row-wrap ${pkt.num === flashPacketNum ? 'pkt-flash' : ''} ${isActive ? 'pkt-border-active' : 'pkt-border-inactive'}`
              return (
                <div className={wrapClasses}>
                  <PacketRow
                    packet={pkt}
                    parameters={pkt.event_id ? packetParametersById?.[pkt.event_id] ?? [] : []}
                    columns={effectiveColumns}
                    selected={isActive}
                    showFrame={showFrame}
                    showEcho={showEcho}
                    compact={compact}
                    onClick={() => onSelect(pkt.num)}
                  />
                </div>
              )
            }}
          />
        </div>
      )}
    </>
  )
}
