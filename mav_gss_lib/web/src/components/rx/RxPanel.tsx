import { useState, useRef, useCallback, useMemo, useEffect } from 'react'
import { useReceivingDetection } from '@/hooks/useReceivingDetection'
import { SessionBanner } from './SessionBanner'
import { ArrowDownToLine } from 'lucide-react'
import { PacketList } from './PacketList'
import { RxPanelHeader } from './RxPanelHeader'
import { RxDetailPane } from './RxDetailPane'
import { useRxDisplayToggles } from '@/state/rxHooks'
import { useColumnDefs } from '@/state/sessionHooks'
import { colors } from '@/lib/colors'
import type { GssConfig, ParamUpdate, RxPacket, RxStatus } from '@/lib/types'

interface RxPanelProps {
  config?: GssConfig | null
  packets: RxPacket[]
  packetParametersById: Record<string, ParamUpdate[]>
  status: RxStatus
  packetStats?: {
    total: number
    crcFailures: number
    dupCount: number
    hasEcho: boolean
  }
  sessionGeneration?: number
  sessionTag?: string
  blackoutUntil?: number | null
}

function hasEcho(packet: RxPacket): boolean {
  return packet.is_echo
}

export function RxPanel({
  config, packets, packetParametersById, status, packetStats, sessionGeneration, sessionTag, blackoutUntil,
}: RxPanelProps) {
  const { showFrame, hideUplink } = useRxDisplayToggles()
  const { defs: columnDefs } = useColumnDefs()
  const rxColumns = columnDefs?.rx
  const [autoScroll, setAutoScroll] = useState(true)
  const [selectedNum, setSelectedNum] = useState<number | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailHeight, setDetailHeight] = useState(200)
  const [isDraggingState, setIsDraggingState] = useState(false)
  const isDragging = useRef(false)
  // Track whether user explicitly pinned a non-latest packet
  const pinned = useRef(false)

  const filtered = useMemo(
    () => hideUplink ? packets.filter(p => !p.is_echo) : packets,
    [packets, hideUplink],
  )
  const showEcho = useMemo(
    () => !hideUplink && (packetStats?.hasEcho ?? packets.some(hasEcho)),
    [hideUplink, packetStats?.hasEcho, packets],
  )
  const lastNum = filtered.length > 0 ? filtered[filtered.length - 1].num : null
  const lastPktNum = packets.length > 0 ? packets[packets.length - 1].num : -1
  const receiving = useReceivingDetection(lastPktNum)

  // Auto-follow latest
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (autoScroll && lastNum !== null && !pinned.current) {
      setSelectedNum(lastNum)
    }
  }, [autoScroll, lastNum])
  /* eslint-enable react-hooks/set-state-in-effect */

  function handleSelect(num: number) {
    if (selectedNum === num && detailOpen) {
      setDetailOpen(false)
      setSelectedNum(null)
    } else if (selectedNum === num && !detailOpen) {
      setDetailOpen(true)
    } else {
      setSelectedNum(num)
      setDetailOpen(true)
      if (num !== lastNum) {
        pinned.current = true
        setAutoScroll(false)
      } else {
        pinned.current = false
      }
    }
  }

  const handleScrolledUp = useCallback(() => setAutoScroll(false), [])

  const scrollToBottom = useCallback(() => {
    pinned.current = false
    setAutoScroll(true)
  }, [])

  const startDragResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    isDragging.current = true
    setIsDraggingState(true)
    const startY = e.clientY
    const startH = detailHeight
    function onMove(ev: MouseEvent) {
      setDetailHeight(Math.max(100, Math.min(window.innerHeight * 0.7, startH + (startY - ev.clientY))))
    }
    function onUp() {
      isDragging.current = false
      setIsDraggingState(false)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [detailHeight])

  const handleResizeKey = useCallback((e: React.KeyboardEvent) => {
    const STEP = 40
    if (e.key === 'PageUp') {
      e.preventDefault()
      setDetailHeight(h => Math.min(window.innerHeight * 0.7, h + STEP))
    } else if (e.key === 'PageDown') {
      e.preventDefault()
      setDetailHeight(h => Math.max(100, h - STEP))
    } else if (e.key === 'Home') {
      e.preventDefault()
      setDetailHeight(200)
    }
  }, [])

  const selectedPacket = selectedNum !== null ? filtered.find(p => p.num === selectedNum) ?? null : null
  const selectedParameters = selectedPacket?.event_id ? packetParametersById[selectedPacket.event_id] ?? [] : []
  const isLive = autoScroll && selectedNum === lastNum

  return (
    <div className="flex flex-col h-full gap-3 relative">
      <div
        className="flex flex-col flex-1 min-h-0 rounded-lg border overflow-hidden shadow-panel"
        style={{
          borderColor: receiving ? `${colors.success}55` : colors.borderSubtle,
          backgroundColor: colors.bgPanel,
          transition: 'border-color 160ms ease',
        }}
      >
        <RxPanelHeader
          config={config}
          status={status}
          packets={packets}
          packetStats={packetStats}
          receiving={receiving}
          blackoutUntil={blackoutUntil}
        />

        <SessionBanner sessionGeneration={sessionGeneration} sessionTag={sessionTag} packetCount={packets.length} />

        <PacketList
          packets={filtered}
          packetParametersById={packetParametersById}
          columns={rxColumns}
          showFrame={showFrame}
          showEcho={showEcho}
          flashPacketNum={lastNum}
          selectedNum={selectedNum}
          onSelect={handleSelect}
          autoScroll={autoScroll}
          onScrolledUp={handleScrolledUp}
          zmqStatus={status.zmq}
          scrollSignal={detailOpen ? detailHeight : -1}
        />

        {!autoScroll && (
          <button
            onClick={scrollToBottom}
            className="flex items-center justify-center gap-1.5 px-3 py-1 text-[11px] font-medium shrink-0 color-transition hover:bg-white/[0.04] btn-feedback"
            style={{ color: colors.warning, backgroundColor: `${colors.warning}08`, borderTop: `1px solid ${colors.warning}22` }}
          >
            <ArrowDownToLine className="size-3" />
            Scroll unlocked — click to resume
          </button>
        )}
      </div>

      {selectedPacket && (
        <RxDetailPane
          packet={selectedPacket}
          parameters={selectedParameters}
          isLive={isLive}
          requiredEbn0Db={config?.platform?.tracking?.link_budget?.required_ebn0_db}
          detailHeight={detailHeight}
          detailOpen={detailOpen}
          isDraggingState={isDraggingState}
          onClose={() => setDetailOpen(false)}
          onStartDragResize={startDragResize}
          onResizeKey={handleResizeKey}
        />
      )}
    </div>
  )
}
