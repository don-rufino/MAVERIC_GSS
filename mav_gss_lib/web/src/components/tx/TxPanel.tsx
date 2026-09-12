import { useState, useMemo, Suspense, useCallback, useRef } from 'react'
import { useEffect } from 'react'
import { useShortcuts } from '@/hooks/useShortcuts'
import { useTabActive } from '@/state/TabActiveContext'
import { motion } from 'framer-motion'
import { FileUp, StopCircle, Send as SendIcon, ShieldCheck, X, Wrench } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Kbd } from '@/components/ui/kbd'
import { StatusDot } from '@/components/shared/atoms/StatusDot'
import { colors, frameColor } from '@/lib/colors'
import { TxQueue } from './TxQueue'
import { CommandInput } from './CommandInput'
import { ImportDialog } from './ImportDialog'
import { getMissionBuilder } from '@/plugins/registry'
import { useColumnDefs } from '@/state/sessionHooks'
import type {
  TxQueueItem, TxQueueSummary,
  SendProgress, GuardConfirm, GssConfig, ColumnDef,
} from '@/lib/types'
import { isOffsetSweepMission } from '@/lib/offsetSweep'

interface TxPanelProps {
  config: GssConfig | null
  queue: TxQueueItem[]
  summary: TxQueueSummary
  sendProgress: SendProgress | null
  guardConfirm: GuardConfirm | null
  connected: boolean
  queueCommand: (line: string) => void
  deleteItem: (index: number) => void
  clearQueue: () => void
  undoLast: () => void
  toggleGuard: (index: number) => void
  reorder: (oldIndex: number, newIndex: number) => void
  addDelay: (ms: number) => void
  addCheckpoint: (text: string) => void
  editDelay: (index: number, ms: number) => void
  sendAll: () => void
  abortSend: () => void
  approveGuard: () => void
  rejectGuard: () => void
  queueTemplate: (payload: Record<string, unknown>) => void
  triggerConfirmSend?: number
  triggerConfirmClear?: number
}

export function TxPanel({
  config, queue, summary, sendProgress, guardConfirm, connected,
  queueCommand, deleteItem, clearQueue,
  toggleGuard, reorder, editDelay, addDelay, addCheckpoint,
  sendAll, abortSend, approveGuard, rejectGuard,
  queueTemplate,
  triggerConfirmSend, triggerConfirmClear,
}: TxPanelProps) {
  const [showBuilder, setShowBuilder] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const { defs: ctxDefs } = useColumnDefs()
  const [cmdHistory, setCmdHistory] = useState<string[]>([])
  const pushHistory = useCallback((cmd: string) => {
    setCmdHistory(prev => [cmd, ...prev])
  }, [])
  const mainCliRef = useRef<HTMLTextAreaElement>(null)

  const missionId = config?.mission.id ?? ''
  /* eslint-disable react-hooks/static-components */
  const MissionBuilder = useMemo(() => getMissionBuilder(missionId), [missionId])
  const hasCommandBuilder = MissionBuilder !== null

  const missionTxColumns = ctxDefs?.tx ?? []
  const txColumns = useMemo(() => {
    if (!isOffsetSweepMission(missionId)) return missionTxColumns
    const offsetCol: ColumnDef = { id: 'tx_offset', label: 'Offset', kind: 'tx_offset', width: 64, align: 'right' }
    const rightIdx = missionTxColumns.findIndex(c => c.align === 'right')
    return rightIdx < 0
      ? [...missionTxColumns, offsetCol]
      : [...missionTxColumns.slice(0, rightIdx), offsetCol, ...missionTxColumns.slice(rightIdx)]
  }, [missionTxColumns, missionId])

  const [frameLabel, setFrameLabel] = useState<string>('')
  useEffect(() => {
    fetch('/api/status').then(r => r.json()).then(s => setFrameLabel(s.frame_label || '')).catch(() => {})
  }, [])

  const sending = sendProgress !== null

  return (
    <div className="flex flex-col h-full gap-2">
      {/* Main card — queue + optional builder split */}
      <div className="flex flex-col flex-1 min-h-0 rounded-lg border overflow-hidden shadow-panel" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
        {/* Panel header */}
        <div className="flex items-center justify-between px-2 py-1 border-b shrink-0" style={{ borderColor: colors.borderSubtle }}>
          <div className="flex items-center gap-2">
            <span className="text-xs font-bold tracking-wide uppercase" style={{ color: colors.value }}>{config?.mission.config.tx_title ?? 'TX Uplink'}</span>
            <StatusDot status={connected ? 'LIVE' : 'DOWN'} />
            <span
              className="text-[11px] font-medium"
              style={{ color: frameColor(frameLabel) }}
            >
              {frameLabel || '--'}
            </span>
            {sending && (
              <Badge className="text-[11px] px-1.5 py-0 h-5 animate-pulse-text" style={{ backgroundColor: `${colors.infoFill}`, color: colors.info }}>
                SENDING {sendProgress.sent}/{sendProgress.total}
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="icon" className="size-6" onClick={() => setShowImport(true)} title="Import commands">
              <FileUp className="size-3.5" style={{ color: colors.dim }} />
            </Button>
          </div>
        </div>

        {/* Queue */}
        <TxQueue
          summary={summary} sendProgress={sendProgress} isGuarding={!!guardConfirm}
          txColumns={txColumns}
          onToggleGuard={toggleGuard} onDelete={deleteItem}
          onEditDelay={editDelay} onAddDelay={addDelay} onAddCheckpoint={addCheckpoint}
          onClear={clearQueue} onSend={sendAll}
          onDuplicate={(idx) => {
            const item = queue[idx]
            if (!item || item.type !== 'mission_cmd') return
            queueTemplate(item.payload)
          }}
          onMoveToTop={(idx) => reorder(idx, 0)}
          onMoveToBottom={(idx) => reorder(idx, queue.length - 1)}
          onRequeue={(item) => queueTemplate(item.payload)}
          triggerConfirmSend={triggerConfirmSend}
          triggerConfirmClear={triggerConfirmClear}
        />
      </div>

      {/* Bottom block: guard confirm / send progress / input+builder */}
      {guardConfirm ? (
        <div className="shrink-0">
          <GuardConfirmBlock
            guardConfirm={guardConfirm}
            onApprove={approveGuard}
            onReject={rejectGuard}
          />
        </div>
      ) : sending ? (
        <div className="shrink-0 flex gap-2 h-[62px]">
          <div className="flex-1 rounded-lg border overflow-hidden flex items-center gap-1.5 px-2 relative" style={{ borderColor: `${colors.info}44`, backgroundColor: `${colors.info}08` }}>
            <motion.div
              className="absolute inset-y-0 left-0"
              style={{ backgroundColor: `${colors.info}15` }}
              initial={{ width: '0%' }}
              animate={{ width: `${(sendProgress.sent / sendProgress.total) * 100}%` }}
              transition={{ type: 'spring', stiffness: 300, damping: 30 }}
            />
            <div className="flex items-center gap-1.5 text-xs font-mono relative z-10">
              <SendIcon className="size-4 animate-pulse-text" style={{ color: colors.info }} />
              <span className="font-bold" style={{ color: colors.info }}>
                Sent {sendProgress.sent}/{sendProgress.total}
              </span>
              {sendProgress.waiting ? (
                <span style={{ color: colors.warning }}>— delay</span>
              ) : (
                <span style={{ color: colors.dim }}>— {sendProgress.current}</span>
              )}
            </div>
          </div>
          <button
            onClick={abortSend}
            className="flex flex-col items-center justify-center gap-0.5 px-5 rounded-lg border text-xs font-bold shrink-0 transition-colors hover:brightness-110 btn-feedback"
            style={{ borderColor: colors.error, color: colors.bgApp, backgroundColor: colors.error }}
          >
            <StopCircle className="size-4" />
            <span className="text-[11px]">Abort</span>
          </button>
        </div>
      ) : (
        <motion.div
          className="rounded-lg border overflow-hidden shadow-float"
          style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgApp }}
          animate={{ height: showBuilder ? '50vh' : 62 }}
          transition={{ type: 'spring', stiffness: 400, damping: 30, mass: 0.8 }}
        >
          {showBuilder && hasCommandBuilder && MissionBuilder ? (
            <div className="flex flex-col h-full">
              <div className="flex-1 overflow-y-auto min-h-0">
                <Suspense fallback={<div className="p-4 text-xs" style={{ color: colors.dim }}>Loading builder...</div>}>
                  <MissionBuilder onQueue={queueTemplate} onClose={() => {
                    setShowBuilder(false)
                    setTimeout(() => mainCliRef.current?.focus(), 50)
                  }} disabled={sending} />
                </Suspense>
              </div>
              <CollapsedCli
                onSubmit={queueCommand}
                history={cmdHistory}
                onHistoryPush={pushHistory}
                onClose={() => {
                  setShowBuilder(false)
                  setTimeout(() => mainCliRef.current?.focus(), 50)
                }}
              />
            </div>
          ) : (
            <div className="flex h-full">
              <div className="flex-1 min-w-0">
                <CommandInput
                  onSubmit={queueCommand}
                  history={cmdHistory}
                  onHistoryPush={pushHistory}
                  ref={mainCliRef}
                  disabled={sending}
                  placeholderOverride={sending ? 'Send in progress — queueing paused' : undefined}
                />
              </div>
              {hasCommandBuilder && MissionBuilder && (
                <button
                  onClick={() => setShowBuilder(true)}
                  className="shrink-0 flex flex-col items-center justify-center gap-1 border-l transition-colors"
                  style={{
                    width: 80,
                    borderColor: colors.borderSubtle,
                    backgroundColor: 'transparent',
                    color: colors.dim,
                  }}
                  onMouseEnter={e => {
                    (e.currentTarget as HTMLButtonElement).style.color = colors.value
                    ;(e.currentTarget as HTMLButtonElement).style.backgroundColor = 'rgba(255,255,255,0.03)'
                  }}
                  onMouseLeave={e => {
                    (e.currentTarget as HTMLButtonElement).style.color = colors.dim
                    ;(e.currentTarget as HTMLButtonElement).style.backgroundColor = 'transparent'
                  }}
                >
                  <Wrench className="size-3.5" />
                  <span className="text-[11px] font-medium">Builder</span>
                </button>
              )}
            </div>
          )}
        </motion.div>
      )}

      {/* Dialogs */}
      <ImportDialog open={showImport} onClose={() => setShowImport(false)} onImported={() => {}} txColumns={txColumns} disabled={sending} />
    </div>
  )
  /* eslint-enable react-hooks/static-components */
}

/* Collapsed CLI bar — shown at the bottom of the builder panel */
function CollapsedCli({ onSubmit, history, onHistoryPush, onClose }: {
  onSubmit: (line: string) => void
  history: string[]
  onHistoryPush: (cmd: string) => void
  onClose: () => void
}) {
  const [value, setValue] = useState('')
  const [histIdx, setHistIdx] = useState(-1)

  return (
    <div
      className="shrink-0 flex items-center gap-1.5 px-2"
      style={{ height: 34, borderTop: `1px solid ${colors.borderSubtle}` }}
    >
      <span className="font-mono text-xs select-none" style={{ color: colors.active, opacity: 0.4 }} aria-hidden="true">❯</span>
      <textarea
        className="flex-1 bg-transparent text-[11px] font-mono outline-none resize-none leading-5"
        style={{ color: colors.value }}
        placeholder="raw command..."
        value={value}
        rows={1}
        onChange={(e) => { setValue(e.target.value); setHistIdx(-1) }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && value.trim()) {
            e.preventDefault()
            onSubmit(value.trim())
            onHistoryPush(value.trim())
            setValue('')
            setHistIdx(-1)
          } else if (e.key === 'ArrowUp' && !value.includes('\n')) {
            e.preventDefault()
            const nextIdx = Math.min(histIdx + 1, history.length - 1)
            setHistIdx(nextIdx)
            if (history[nextIdx]) setValue(history[nextIdx])
          } else if (e.key === 'ArrowDown' && !value.includes('\n')) {
            e.preventDefault()
            const nextIdx = histIdx - 1
            if (nextIdx < 0) { setHistIdx(-1); setValue('') }
            else { setHistIdx(nextIdx); setValue(history[nextIdx]) }
          }
        }}
        spellCheck={false}
        autoComplete="off"
      />
      <Kbd className="!h-[18px] !min-w-[18px] !text-[10px]">↑</Kbd>
      <Kbd className="!h-[18px] !min-w-[18px] !text-[10px]">↓</Kbd>
      <span style={{ color: colors.sep, fontSize: 10 }}>·</span>
      <button
        onClick={onClose}
        className="text-[10px] bg-transparent border-none cursor-pointer"
        style={{ color: colors.dim }}
      >close</button>
    </div>
  )
}

/* Guard confirm inline block */
function GuardConfirmBlock({ guardConfirm, onApprove, onReject }: {
  guardConfirm: GuardConfirm
  onApprove: () => void
  onReject: () => void
}) {
  // Enter to approve, Escape to reject
  useShortcuts([
    { key: 'Enter', action: onApprove },
    { key: 'Escape', action: onReject },
  ], useTabActive())

  const checkpoint = guardConfirm.kind === 'checkpoint'
  const title = checkpoint ? 'CHECKPOINT - Confirm to continue' : 'GUARD - Confirm to send'
  const detail = guardConfirm.text || guardConfirm.cmd_id

  return (
    <div
      className="rounded-lg border animate-pulse-warning h-[62px] flex overflow-hidden"
      style={{ borderColor: `${colors.warning}44` }}
    >
      {/* Info */}
      <div className="flex-1 flex items-center gap-2 px-2">
        <ShieldCheck className="size-5 shrink-0 animate-pulse-text" style={{ color: colors.warning }} />
        <div className="min-w-0">
          <div className="text-xs font-bold" style={{ color: colors.warning }}>{title}</div>
          <div className="text-[11px] truncate" style={{ color: colors.value }}>
            {detail}
          </div>
        </div>
      </div>

      {/* Reject */}
      <button
        onClick={onReject}
        className="px-3 flex items-center justify-center border-l transition-colors hover:bg-white/[0.05]"
        style={{ borderColor: colors.borderSubtle, color: colors.dim }}
      >
        <X className="size-4" />
      </button>

      {/* Approve — full height block */}
      <button
        onClick={onApprove}
        className="px-5 flex flex-col items-center justify-center gap-0.5 transition-colors hover:brightness-110 btn-feedback"
        style={{ backgroundColor: colors.warning, color: colors.bgApp }}
      >
        <ShieldCheck className="size-4" />
        <span className="text-[11px] font-bold">Confirm Enter</span>
      </button>
    </div>
  )
}
