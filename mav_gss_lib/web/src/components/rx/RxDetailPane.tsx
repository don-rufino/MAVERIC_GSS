import { useCallback } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X, ClipboardCopy, Binary } from 'lucide-react'
import {
  ContextMenuRoot, ContextMenuTrigger, ContextMenuContent,
  ContextMenuItem,
} from '@/components/shared/overlays/ContextMenu'
import { PacketDetail } from './PacketDetail'
import { SignalCard } from './SignalCard'
import { useRxDisplayToggles } from '@/state/rxHooks'
import {
  integrityBlocks,
  packetDisplayLabel,
  packetPayloadText,
  protocolBlocks,
  rxTimestamp,
} from '@/lib/rxPacket'
import { colors } from '@/lib/colors'
import type { ParamUpdate, RxPacket } from '@/lib/types'

function f(label: string, value: string): string {
  return `  ${label.padEnd(12)} ${value}`
}

function extractCmd(p: RxPacket): string {
  return packetDisplayLabel(p)
}

function extractCmdArgs(p: RxPacket, parameters: ParamUpdate[]): string {
  return [packetDisplayLabel(p), packetPayloadText(p, { parameters })].filter(Boolean).join(' ').trim()
}

function formatPacketText(p: RxPacket, parameters: ParamUpdate[]): string {
  const lines: string[] = []
  const sep = '\u2500'
  const extras = [p.frame || '', `${p.size}B`, p.is_dup ? '[DUP]' : '', p.is_echo ? '[UL]' : ''].filter(Boolean).join('  ')
  lines.push(`${sep.repeat(4)} #${p.num}  ${rxTimestamp(p)}  ${extras} ${sep.repeat(20)}`)
  if (p.is_echo) lines.push('  \u25B2\u25B2\u25B2 UPLINK ECHO \u25B2\u25B2\u25B2')
  for (const w of p.warnings) lines.push(f('\u26A0 WARNING', w))

  const label = packetDisplayLabel(p)
  if (label) lines.push(f('LABEL', label))
  const payload = packetPayloadText(p, { parameters })
  if (payload) lines.push(f('PAYLOAD', payload))

  for (const block of protocolBlocks(p)) {
    const vals = (block.fields ?? []).map((fld: { name: string; value: string }) => `${fld.name}:${fld.value}`).join('  ')
    lines.push(f(block.label, vals))
  }

  for (const block of integrityBlocks(p)) {
    lines.push(f(block.label, block.ok === null ? '?' : block.ok ? 'OK' : 'FAIL'))
  }

  if (p.raw_hex) {
    const hex = p.raw_hex.match(/.{1,2}/g)?.join(' ') ?? p.raw_hex
    const chunks = hex.match(/.{1,47}/g) ?? [hex]
    chunks.forEach((chunk, i) => lines.push(i === 0 ? f('HEX', chunk) : f('', chunk)))
  }
  return lines.join('\n')
}

interface RxDetailPaneProps {
  packet: RxPacket
  parameters: ParamUpdate[]
  isLive: boolean
  requiredEbn0Db?: number
  detailHeight: number
  detailOpen: boolean
  isDraggingState: boolean
  onClose: () => void
  onStartDragResize: (e: React.MouseEvent) => void
  onResizeKey: (e: React.KeyboardEvent) => void
}

export function RxDetailPane({
  packet, parameters, isLive, requiredEbn0Db,
  detailHeight, detailOpen, isDraggingState,
  onClose, onStartDragResize, onResizeKey,
}: RxDetailPaneProps) {
  const { showHex, showFrame, showWrapper } = useRxDisplayToggles()
  const signalCard = packet.signal
    ? <SignalCard signal={packet.signal} requiredEbn0Db={requiredEbn0Db} />
    : null

  const copyDetails = useCallback(() => {
    navigator.clipboard.writeText(formatPacketText(packet, parameters))
  }, [packet, parameters])
  const copyCmdArgs = useCallback(() => {
    navigator.clipboard.writeText(extractCmdArgs(packet, parameters))
  }, [packet, parameters])
  const copyHex = useCallback(() => {
    if (packet.raw_hex) navigator.clipboard.writeText(packet.raw_hex)
  }, [packet])

  return (
    <div
      className="shrink-0 overflow-hidden"
      style={{
        height: detailOpen ? detailHeight : 0,
        opacity: detailOpen ? 1 : 0,
        transition: isDraggingState ? 'none' : 'height 0.2s ease, opacity 0.15s ease',
      }}
    >
      <ContextMenuRoot>
        <ContextMenuTrigger>
          <div
            className="flex flex-col rounded-lg border overflow-hidden shadow-panel"
            style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel, height: detailHeight }}
          >
            <div
              onMouseDown={onStartDragResize}
              onKeyDown={onResizeKey}
              role="separator"
              aria-orientation="horizontal"
              aria-label="Resize packet detail pane — PageUp/PageDown to adjust, Home to reset"
              title="Drag to resize · PageUp/PageDown"
              tabIndex={0}
              className="h-2 shrink-0 cursor-ns-resize flex items-center justify-center relative focus:outline-none focus-visible:ring-2 focus-visible:ring-[#E8B83A]"
              style={{ backgroundColor: colors.bgPanelRaised }}
            >
              {/* Expanded hit zone — 16px total click target (8px visible + 8px above) per HFDS 9.5.1 */}
              <span aria-hidden="true" className="absolute inset-x-0 -top-2 h-2" />
              <div className="w-8 h-0.5 rounded-full" style={{ backgroundColor: '#606060' }} />
            </div>
            <div className="flex items-center justify-between px-3 py-1 border-b shrink-0" style={{ borderColor: colors.borderSubtle }}>
              <span className="text-xs font-bold" style={{ color: colors.value }}>
                #{packet.num} {extractCmd(packet)}
                {isLive && <span className="ml-2 text-[11px] font-normal" style={{ color: colors.success }}>LIVE</span>}
              </span>
              <button onClick={onClose} className="p-0.5 rounded hover:bg-white/5">
                <X className="size-3" style={{ color: colors.dim }} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto">
              <AnimatePresence mode="wait">
                <motion.div
                  key={packet.num}
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.1, ease: 'easeOut' }}
                >
                  <PacketDetail packet={packet} parameters={parameters} showHex={showHex} showWrapper={showWrapper} showFrame={showFrame} signalCard={signalCard} />
                </motion.div>
              </AnimatePresence>
            </div>
          </div>
        </ContextMenuTrigger>
        <ContextMenuContent>
          <ContextMenuItem icon={ClipboardCopy} onSelect={copyDetails}>
            Copy Full Details
          </ContextMenuItem>
          <ContextMenuItem icon={ClipboardCopy} onSelect={copyCmdArgs}>
            Copy Command + Args
          </ContextMenuItem>
          {packet.raw_hex && (
            <ContextMenuItem icon={Binary} onSelect={copyHex}>
              Copy Hex
            </ContextMenuItem>
          )}
        </ContextMenuContent>
      </ContextMenuRoot>
    </div>
  )
}
