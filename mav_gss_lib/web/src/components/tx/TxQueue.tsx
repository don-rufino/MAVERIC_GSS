import { useState, useEffect, useRef, useMemo } from 'react'
import {
  DndContext, closestCenter,
  PointerSensor, useSensor, useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { Trash2, Send, Timer, Save, ArrowDownToLine, ArrowUpToLine, History, ShieldCheck } from 'lucide-react'
import { useTabActive } from '@/state/TabActiveContext'
import { useShortcuts, type Shortcut } from '@/hooks/useShortcuts'
import { useFollowScroll } from '@/hooks/useFollowScroll'
import { isInputFocused } from '@/lib/utils'
import { PromptDialog } from '@/components/shared/dialogs/PromptDialog'
import { showToast } from '@/components/shared/overlays/StatusToast'
import { authFetch } from '@/lib/auth'
import {
  ContextMenuRoot, ContextMenuTrigger, ContextMenuContent,
  ContextMenuItem, ContextMenuSeparator,
} from '@/components/shared/overlays/ContextMenu'
import { Button } from '@/components/ui/button'
import { ConfirmBar } from '@/components/shared/overlays/ConfirmBar'
import { QueueItem } from './QueueItem'
import { DelayItem } from './DelayItem'
import { NoteItem } from './NoteItem'
import { CheckpointItem } from './CheckpointItem'
import { colors } from '@/lib/colors'
import { col, buildTxRow, columnWidthClass, columnWidthStyle, columnAlignClass } from '@/lib/columns'
import { useTx } from '@/state/txHooks'
import type {
  TxQueueSummary, SendProgress, ColumnDef, TxHistoryItem, TxQueueCmd,
} from '@/lib/types'

interface TxQueueProps {
  summary: TxQueueSummary
  sendProgress: SendProgress | null
  isGuarding: boolean
  txColumns: ColumnDef[]
  onToggleGuard: (index: number) => void
  onDelete: (index: number) => void
  onEditDelay: (index: number, ms: number) => void
  onAddDelay: (ms: number) => void
  onAddCheckpoint: (text: string) => void
  onClear: () => void
  onSend: () => void
  onDuplicate: (index: number) => void
  onMoveToTop: (index: number) => void
  onMoveToBottom: (index: number) => void
  onRequeue: (item: TxHistoryItem) => void
  triggerConfirmSend?: number
  triggerConfirmClear?: number
}

export function TxQueue({
  summary, sendProgress, isGuarding,
  txColumns,
  onToggleGuard, onDelete, onEditDelay, onAddDelay, onAddCheckpoint,
  onClear, onSend, onDuplicate, onMoveToTop, onMoveToBottom, onRequeue,
  triggerConfirmSend, triggerConfirmClear,
}: TxQueueProps) {
  const tabActive = useTabActive()
  const { items, applyDragReorder } = useTx()

  const [selectedUid, setSelectedUid] = useState<string | null>(null)
  const [focusedIdx, setFocusedIdx] = useState<number | null>(null)
  const [localConfirmClear, setLocalConfirmClear] = useState(false)
  const [localConfirmSend, setLocalConfirmSend] = useState(false)
  const [handledConfirmClearSignal, setHandledConfirmClearSignal] = useState(0)
  const [handledConfirmSendSignal, setHandledConfirmSendSignal] = useState(0)
  const [confirmClearSent, setConfirmClearSent] = useState(false)
  const [showSavePrompt, setShowSavePrompt] = useState(false)
  const [showCheckpointPrompt, setShowCheckpointPrompt] = useState(false)

  const outerRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const sendSignal = triggerConfirmSend ?? 0
  const clearSignal = triggerConfirmClear ?? 0
  const signalConfirmSend = tabActive && sendSignal > 0 && sendSignal !== handledConfirmSendSignal
  const signalConfirmClear = tabActive && clearSignal > 0 && clearSignal !== handledConfirmClearSignal
  const confirmSend = localConfirmSend || signalConfirmSend
  const confirmClear = localConfirmClear || signalConfirmClear

  const closeConfirmSend = () => {
    setLocalConfirmSend(false)
    setHandledConfirmSendSignal(sendSignal)
  }
  const closeConfirmClear = () => {
    setLocalConfirmClear(false)
    setHandledConfirmClearSignal(clearSignal)
  }

  const sendingItem = items.find(i => i.status === 'sending')
  const sendTargetUid = sendingItem?.uid ?? null
  const resetKey: 'idle' | 'active' = sendProgress === null ? 'idle' : 'active'
  const { detached, direction, jumpToCurrent } = useFollowScroll({
    containerRef: scrollRef,
    target: sendTargetUid,
    resetKey,
  })

  const pendingItems = useMemo(() => items.filter(i => i.source === 'queue'), [items])

  const prevPendingCountRef = useRef(pendingItems.length)
  useEffect(() => {
    const prev = prevPendingCountRef.current
    prevPendingCountRef.current = pendingItems.length
    if (pendingItems.length > prev) {
      const c = scrollRef.current
      if (c) c.scrollTo({ top: c.scrollHeight, behavior: 'smooth' })
    }
  }, [pendingItems.length])

  // When a send completes, the TxBuilder/CLI block slides back in via a
  // Framer Motion spring (~500 ms), so the queue's container height
  // shrinks gradually. A one-shot scrollTo fires before the layout has
  // actually reflowed, leaving the just-sent row stranded mid-list. Pin
  // scrollTop to scrollHeight every frame for the full settle window so
  // the bottom stays glued through the whole animation.
  const prevSendingRef = useRef(sendProgress !== null)
  useEffect(() => {
    const wasSending = prevSendingRef.current
    const isSending = sendProgress !== null
    prevSendingRef.current = isSending
    if (!(wasSending && !isSending)) return
    const c = scrollRef.current
    if (!c) return
    let raf = 0
    const start = performance.now()
    const pin = () => {
      const el = scrollRef.current
      if (el) el.scrollTop = el.scrollHeight
      if (performance.now() - start < 700) {
        raf = requestAnimationFrame(pin)
      }
    }
    raf = requestAnimationFrame(pin)
    return () => { if (raf) cancelAnimationFrame(raf) }
  }, [sendProgress])

  const queueShortcuts = useMemo<Shortcut[]>(() => [
    {
      key: 'ArrowUp',
      action: () => setFocusedIdx(prev => prev === null ? 0 : Math.max(0, prev - 1)),
      when: () => pendingItems.length > 0 && !isInputFocused(),
    },
    {
      key: 'ArrowDown',
      action: () => setFocusedIdx(prev => {
        const max = pendingItems.length - 1
        return prev === null ? 0 : Math.min(max, prev + 1)
      }),
      when: () => pendingItems.length > 0 && !isInputFocused(),
    },
    {
      key: 'Delete',
      action: () => { if (focusedIdx !== null) onDelete(focusedIdx) },
      when: () => focusedIdx !== null && pendingItems.length > 0 && !isInputFocused(),
    },
    {
      key: 'Backspace',
      action: () => { if (focusedIdx !== null) onDelete(focusedIdx) },
      when: () => focusedIdx !== null && pendingItems.length > 0 && !isInputFocused(),
    },
    {
      key: 'g',
      action: () => { if (focusedIdx !== null) onToggleGuard(focusedIdx) },
      when: () => focusedIdx !== null && pendingItems.length > 0 && !isInputFocused(),
    },
    {
      key: 'G',
      shift: true,
      action: () => { if (focusedIdx !== null) onToggleGuard(focusedIdx) },
      when: () => focusedIdx !== null && pendingItems.length > 0 && !isInputFocused(),
    },
  ], [pendingItems.length, focusedIdx, onDelete, onToggleGuard])

  useShortcuts(queueShortcuts, tabActive)

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  )

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event
    if (!over) return
    // applyDragReorder is atomic: validates pending-only, computes the new
    // absolute order (override-aware), installs the override, and submits
    // the full `order` array to the backend. Returns false if the drop is
    // rejected (cross-segment or unknown uid).
    const applied = applyDragReorder(String(active.id), String(over.id))
    if (applied) setSelectedUid(null)
  }

  const visibleColumns = useMemo(() => {
    return txColumns.filter(c => {
      if (!c.hide_if_all?.length) return true
      const suppressSet = new Set(c.hide_if_all)
      if (items.length === 0) return true
      return !items.every(it => {
        if (it.source === 'queue' && (it.item.type === 'delay' || it.item.type === 'note' || it.item.type === 'checkpoint')) return true
        const row = buildTxRow(it.item as TxQueueCmd | TxHistoryItem, [c])
        return suppressSet.has(row[c.id]?.value as never)
      })
    })
  }, [txColumns, items])

  const estStr = summary.est_time_s > 0 ? `~${summary.est_time_s.toFixed(1)}s` : ''
  const hasSent = items.some(i => i.source === 'history')
  const hasPending = pendingItems.length > 0
  const isActivelySending = sendProgress !== null

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      {items.length > 0 && (
        <div className="flex items-center gap-1 px-1.5 py-0.5 text-[11px] font-light shrink-0" style={{ color: colors.dim }}>
          <span className={`${col.grip} shrink-0`} />
          <span className={`${col.num} shrink-0 text-right tabular-nums`}>#</span>
          {visibleColumns.length > 0
            ? visibleColumns.map(c => (
                <span key={c.id} className={`py-1 px-1 ${columnWidthClass(c)} ${columnAlignClass(c)} whitespace-nowrap`} style={columnWidthStyle(c)}>{c.label}</span>
              ))
            : <span className="flex-1">command</span>
          }
          <span className={`${col.actions} shrink-0 ml-1`} />
        </div>
      )}
      <ContextMenuRoot>
      <ContextMenuTrigger>
      <div ref={outerRef} className="relative flex-1 min-h-0 flex flex-col">
        <div ref={scrollRef} className="flex-1 overflow-y-auto overflow-x-hidden py-0.5">
          {items.length === 0 ? (
            <div className="flex items-center justify-center h-full text-xs" style={{ color: colors.dim }}>
              Queue empty — type a command below
            </div>
          ) : (
            <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
              <SortableContext items={items.map(i => i.uid)} strategy={verticalListSortingStrategy}>
                {items.map((it) => {
                  if (it.source === 'queue' && it.item.type === 'delay') {
                    const delayActive = sendProgress !== null && sendProgress.waiting === true && it.queueIndex === 0
                    return (
                      <div key={it.uid}>
                        <DelayItem
                          delayMs={it.item.delay_ms}
                          index={it.queueIndex!}
                          sortId={it.uid}
                          isActive={delayActive}
                          status={it.status}
                          onEditDelay={onEditDelay}
                          onDelete={onDelete}
                        />
                      </div>
                    )
                  }
                  if (it.source === 'queue' && it.item.type === 'note') {
                    return (
                      <div key={it.uid}>
                        <NoteItem
                          text={it.item.text}
                          index={it.queueIndex!}
                          sortId={it.uid}
                          status={it.status}
                          onDelete={onDelete}
                        />
                      </div>
                    )
                  }
                  if (it.source === 'queue' && it.item.type === 'checkpoint') {
                    const checkpointActive = isGuarding && it.status === 'sending'
                    return (
                      <div key={it.uid}>
                        <CheckpointItem
                          text={it.item.text}
                          index={it.queueIndex!}
                          sortId={it.uid}
                          status={it.status}
                          isActive={checkpointActive}
                          onDelete={onDelete}
                        />
                      </div>
                    )
                  }
                  const itemGuarding = isGuarding && it.status === 'sending'
                  const idx = it.queueIndex ?? 0
                  const handle = (it.item as TxQueueCmd | TxHistoryItem)
                  return (
                    <div key={it.uid}>
                      <QueueItem
                        item={handle}
                        status={it.status}
                        index={idx}
                        sortId={it.uid}
                        expanded={selectedUid === it.uid}
                        isGuarding={itemGuarding}
                        visibleColumns={visibleColumns}
                        onSelect={() => setSelectedUid(selectedUid === it.uid ? null : it.uid)}
                        onToggleGuard={onToggleGuard}
                        onDelete={onDelete}
                        onDuplicate={onDuplicate}
                        onMoveToTop={onMoveToTop}
                        onMoveToBottom={onMoveToBottom}
                        onRequeue={onRequeue}
                      />
                    </div>
                  )
                })}
              </SortableContext>
            </DndContext>
          )}
        </div>

        {detached && sendTargetUid && (
          <button
            onClick={jumpToCurrent}
            className={`absolute ${direction === 'up' ? 'top-2' : 'bottom-2'} right-3 flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium shadow-float btn-feedback`}
            style={{ backgroundColor: colors.info, color: colors.bgApp, zIndex: 20 }}
          >
            {direction === 'up'
              ? <ArrowUpToLine className="size-3" />
              : <ArrowDownToLine className="size-3" />}
            Jump to current
          </button>
        )}
      </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem icon={Save} onSelect={() => setShowSavePrompt(true)}>
          Save Queue
        </ContextMenuItem>
        {hasSent && !isActivelySending && (
          <>
            <ContextMenuSeparator />
            <ContextMenuItem
              icon={History}
              destructive
              onSelect={() => setConfirmClearSent(true)}
            >
              Clear Sent
            </ContextMenuItem>
          </>
        )}
      </ContextMenuContent>
      </ContextMenuRoot>

      {confirmClear ? (
        <ConfirmBar
          label={`Clear ${summary.cmds} pending command${summary.cmds !== 1 ? 's' : ''}?`}
          color={colors.error}
          onConfirm={() => { onClear(); closeConfirmClear() }}
          onCancel={closeConfirmClear}
        />
      ) : confirmClearSent ? (
        <ConfirmBar
          label="Clear all sent history?"
          color={colors.error}
          onConfirm={() => {
            authFetch('/api/tx/clear-sent', { method: 'POST' })
              .then(r => r.json())
              .then(d => {
                if (d.ok) showToast(`Cleared ${d.cleared} sent`, 'success')
                else showToast(d.error || 'Clear failed', 'error')
              })
              .catch(() => showToast('Clear failed', 'error'))
            setConfirmClearSent(false)
          }}
          onCancel={() => setConfirmClearSent(false)}
        />
      ) : confirmSend ? (
        <ConfirmBar
          label={`Send ${summary.cmds} command${summary.cmds !== 1 ? 's' : ''}?`}
          color={colors.success}
          onConfirm={() => { onSend(); closeConfirmSend() }}
          onCancel={closeConfirmSend}
        />
      ) : (
        <div className="flex items-center justify-between px-2 py-0.5 border-t shrink-0" style={{ borderColor: colors.borderSubtle }}>
          <span className="text-[11px]" style={{ color: colors.dim }}>
            {summary.cmds} pending
            {summary.guards > 0 ? ` · ${summary.guards} guarded` : ''}
            {summary.checkpoints ? ` · ${summary.checkpoints} checkpoint${summary.checkpoints !== 1 ? 's' : ''}` : ''}
            {estStr ? ` · ${estStr}` : ''}
          </span>
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="sm" onClick={() => onAddDelay(2000)} className="h-6 px-2 text-xs gap-1" style={{ color: colors.dim }} disabled={!hasPending}>
              <Timer className="size-3" /> Delay
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setShowCheckpointPrompt(true)} className="h-6 px-2 text-xs gap-1" style={{ color: colors.dim }} disabled={isActivelySending}>
              <ShieldCheck className="size-3" /> Checkpoint
            </Button>
            <Button variant="ghost" size="sm" onClick={() => { setLocalConfirmClear(true); setLocalConfirmSend(false) }}
              className="h-6 px-2 text-xs gap-1" style={{ color: colors.dim }} disabled={!hasPending}>
              <Trash2 className="size-3" /> Clear
            </Button>
            <Button size="sm" onClick={() => { setLocalConfirmSend(true); setLocalConfirmClear(false) }}
              className="h-6 px-2 text-xs gap-1 btn-feedback" disabled={!hasPending || isActivelySending}
              style={{ color: colors.bgBase, backgroundColor: colors.success }}>
              <Send className="size-3" /> Send All
            </Button>
          </div>
        </div>
      )}
        <PromptDialog
          open={showSavePrompt}
          title="Save Queue"
          placeholder="File name (optional)"
          onSubmit={(name) => {
            setShowSavePrompt(false)
            authFetch('/api/export-queue', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name }),
            }).then(r => r.json()).then(d => {
            if (d.ok) showToast(`Saved ${d.count} commands as ${d.filename}`, 'success')
            else showToast(d.error || 'Save failed', 'error')
          }).catch(() => showToast('Failed to save queue', 'error'))
        }}
        onCancel={() => setShowSavePrompt(false)}
      />
      <PromptDialog
        open={showCheckpointPrompt}
        title="Add Checkpoint"
        placeholder="Checkpoint text"
        onSubmit={(text) => {
          setShowCheckpointPrompt(false)
          onAddCheckpoint(text)
        }}
        onCancel={() => setShowCheckpointPrompt(false)}
      />
    </div>
  )
}
