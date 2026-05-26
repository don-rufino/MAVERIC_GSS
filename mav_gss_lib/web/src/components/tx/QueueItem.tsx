import { useEffect, useState } from 'react'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  GripVertical, Shield, ShieldCheck, Trash2, Copy, ArrowUpToLine,
  ArrowDownToLine, RotateCcw,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { colors } from '@/lib/colors'
import { col, buildTxRow, columnWidthClass, columnWidthStyle, columnAlignClass } from '@/lib/columns'
import {
  ContextMenuRoot, ContextMenuTrigger, ContextMenuContent,
  ContextMenuItem, ContextMenuSeparator,
} from '@/components/shared/overlays/ContextMenu'
import { CellValue } from '@/components/shared/rendering'
import { useTx } from '@/state/txHooks'
import { VerifierTickStrip } from './VerifierTickStrip'
import { VerifierDetailBlock } from './VerifierDetailBlock'
import { txDetailBlocks, txParameterBlocks } from '@/lib/txDetail'
import type { ColumnDef, TxQueueCmd, TxHistoryItem, TxRowStatus } from '@/lib/types'

const TERMINAL: TxRowStatus[] = ['complete', 'failed', 'timed_out']
const isTerminal = (s: TxRowStatus) => TERMINAL.includes(s)

function railColor(status: TxRowStatus, guard: boolean, isGuarding: boolean): string {
  if (isGuarding) return colors.warning
  if (status === 'released') return colors.warning
  if (status === 'accepted') return colors.success
  if (status === 'complete') return colors.success
  if (status === 'failed') return colors.danger
  if (status === 'timed_out') return colors.danger
  return guard ? colors.warning : colors.borderStrong
}

interface QueueItemProps {
  item: TxQueueCmd | TxHistoryItem
  status: TxRowStatus
  index: number            // queue index (pending) or 0 placeholder (terminal)
  sortId: string
  expanded: boolean
  isGuarding: boolean
  flash?: boolean
  compact?: boolean
  visibleColumns: ColumnDef[]
  onSelect: () => void
  onToggleGuard: (index: number) => void
  onDelete: (index: number) => void
  onDuplicate: (index: number) => void
  onMoveToTop: (index: number) => void
  onMoveToBottom: (index: number) => void
  onRequeue?: (item: TxHistoryItem) => void
}

export function QueueItem({
  item, status, index, sortId, expanded, isGuarding, flash, compact,
  visibleColumns, onSelect, onToggleGuard, onDelete, onDuplicate,
  onMoveToTop, onMoveToBottom, onRequeue,
}: QueueItemProps) {
  const pending = status === 'pending' || status === 'sending'

  const {
    attributes, listeners, setNodeRef, transform, transition, isDragging,
  } = useSortable({ id: sortId, disabled: !pending })

  const num = 'num' in item ? item.num : ('n' in item ? item.n : 0)
  const guard = 'guard' in item ? item.guard : false
  const row = buildTxRow(item, visibleColumns)

  const { verification } = useTx()
  const cmdEventId = (item as { event_id?: string }).event_id ?? ''
  const instance = cmdEventId ? (verification.get(cmdEventId) ?? null) : null

  const effectiveStatus: TxRowStatus = (() => {
    if (instance) {
      if (instance.stage === 'received') return 'accepted'
      return instance.stage as TxRowStatus
    }
    return status
  })()
  const effectiveTerminal = isTerminal(effectiveStatus)

  const [nowMs, setNowMs] = useState(() => Date.now())
  const liveInstance = !!instance && !effectiveTerminal
  useEffect(() => {
    if (!liveInstance) return
    const id = window.setInterval(() => setNowMs(Date.now()), 500)
    return () => window.clearInterval(id)
  }, [liveInstance])

  const dotsCaution = !!instance && instance.verifier_set.verifiers.some(v => {
    const o = instance.outcomes[v.verifier_id]
    if (!o || o.state !== 'pending') return false
    return (nowMs - instance.t0_ms) / v.check_window.stop_ms > 0.8
  })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : (effectiveTerminal ? 0.6 : 1),
  }

  const borderColor = railColor(effectiveStatus, guard, isGuarding)
  const pulseClass = (() => {
    if (!liveInstance) return ''
    if (dotsCaution) return 'animate-pulse-warning'
    return 'animate-pulse-info'
  })()

  const protocolBlocks = txDetailBlocks(item)
  const argBlocks = txParameterBlocks(item)
  const cmdLabel = item.cmd_id || 'unknown'

  // Split columns at the first right-aligned one so badges can sit in
  // the leftover gap (before ml-auto absorbs it) without pushing
  // verify+actions sideways. Keeps trailing controls at consistent
  // x-positions across rows.
  const splitIdx = visibleColumns.findIndex(c => c.align === 'right')
  const preCols = splitIdx < 0 ? visibleColumns : visibleColumns.slice(0, splitIdx)
  const rightCols = splitIdx < 0 ? [] : visibleColumns.slice(splitIdx)
  const renderColumn = (c: ColumnDef) => {
    if (c.kind === 'verifiers') {
      return (
        <span key={c.id} className={`py-1 px-1 ${columnWidthClass(c)} ${columnAlignClass(c)}`} style={columnWidthStyle(c)}>
          <VerifierTickStrip instance={instance} now_ms={nowMs} />
        </span>
      )
    }
    return <CellValue key={c.id} col={c} row={row} showFrame showEcho />
  }

  return (
    <ContextMenuRoot>
      <ContextMenuTrigger>
        <div
          ref={setNodeRef}
          data-follow-id={sortId}
          style={{
            ...style,
            ...(compact ? {} : { borderLeftColor: borderColor, borderLeftWidth: liveInstance ? '4px' : undefined }),
          }}
          className={`color-transition rounded-md text-xs ${compact ? '' : 'border-l-2'} mb-0.5 hover:bg-white/[0.03] ${pulseClass} ${flash ? 'animate-slide-in' : ''}`}
        >
          <div className="flex items-center gap-1 px-1.5 py-0.5 cursor-pointer" onClick={onSelect}>
            {!compact && (
              pending ? (
                <span {...attributes} {...listeners}
                  className={`${col.grip} cursor-grab shrink-0 p-0.5 rounded hover:bg-white/[0.06] flex items-center justify-center`}
                  onClick={(e) => e.stopPropagation()}
                >
                  <GripVertical className="size-3.5" style={{ color: colors.dim }} />
                </span>
              ) : (
                <span className={`${col.grip} shrink-0`} aria-hidden="true" />
              )
            )}
            <span className={`${col.num} text-right shrink-0 tabular-nums`} style={{ color: colors.dim }}>{num}</span>
            {preCols.map(renderColumn)}
            {/* Right-anchored group: row badges sit immediately left of verify
                so they share the right edge with the verifier and actions.
                Wrapper carries ml-auto so verify+actions x-positions stay
                constant regardless of which badges show. */}
            <div className="ml-auto flex items-center gap-1 shrink-0">
              {!compact && isGuarding && (
                <Badge className="text-[11px] px-1.5 py-0 h-5 shrink-0 animate-pulse-warning" style={{ backgroundColor: `${colors.warning}22`, color: colors.warning }}>GUARDING</Badge>
              )}
              {!compact && index === 0 && !liveInstance && status !== 'complete' && !isGuarding && (
                <Badge className="text-[11px] px-1.5 py-0 h-5 shrink-0" style={{ backgroundColor: `${colors.label}22`, color: colors.label }}>NEXT</Badge>
              )}
              {!compact && status === 'pending' && guard && index !== 0 && (
                <Badge className="text-[11px] px-1.5 py-0 h-5 shrink-0" style={{ backgroundColor: `${colors.warning}22`, color: colors.warning }}>GUARD</Badge>
              )}
              {rightCols.map(renderColumn)}
            </div>
            {!compact && (
              pending ? (
                <div className={`${col.actions} flex items-center gap-0.5 shrink-0 ml-1 justify-end`}>
                  <Button variant="ghost" size="icon" className="size-5 rounded-md btn-feedback"
                    onClick={(e) => { e.stopPropagation(); onToggleGuard(index) }}
                    title={guard ? 'Remove guard' : 'Add guard'}>
                    {guard
                      ? <ShieldCheck className="size-3.5" style={{ color: colors.warning }} />
                      : <Shield className="size-3.5" style={{ color: colors.dim }} />
                    }
                  </Button>
                  <Button variant="ghost" size="icon" className="size-5 rounded-md btn-feedback"
                    onClick={(e) => { e.stopPropagation(); onDelete(index) }} title="Delete">
                    <Trash2 className="size-3.5" style={{ color: colors.dim }} />
                  </Button>
                </div>
              ) : (
                <span className={`${col.actions} shrink-0 ml-1`} aria-hidden="true" />
              )
            )}
          </div>

          {expanded && (
            <div className="px-2 pb-1 pt-0.5 ml-[54px] space-y-1 animate-slide-in" style={{ borderTop: `1px solid ${colors.borderSubtle}` }}>
              {[...argBlocks, ...protocolBlocks].map((block, i) => (
                <div key={i} className="space-y-0.5">
                  <div className="text-[11px] font-bold uppercase tracking-wide" style={{ color: colors.dim }}>{block.label}</div>
                  {block.fields.map((f, j) => (
                    <div key={j} className="flex items-center gap-1.5 text-xs">
                      <span style={{ color: colors.label }}>{f.name}</span>
                      <span style={{ color: colors.sep }}>=</span>
                      <span style={{ color: colors.value }}>{f.value}</span>
                    </div>
                  ))}
                </div>
              ))}
              {pending && guard && <div style={{ color: colors.warning }} className="text-[11px]">Guarded — requires confirmation</div>}
              {instance && <VerifierDetailBlock instance={instance} now_ms={nowMs} />}
            </div>
          )}
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        {pending ? (
          <>
            <ContextMenuItem icon={guard ? Shield : ShieldCheck} onSelect={() => onToggleGuard(index)}>
              {guard ? 'Remove Guard' : 'Add Guard'}
            </ContextMenuItem>
            <ContextMenuItem icon={Copy} onSelect={() => onDuplicate(index)}>
              Duplicate
            </ContextMenuItem>
            <ContextMenuItem icon={ArrowUpToLine} onSelect={() => onMoveToTop(index)}>
              Move to Top
            </ContextMenuItem>
            <ContextMenuItem icon={ArrowDownToLine} onSelect={() => onMoveToBottom(index)}>
              Move to Bottom
            </ContextMenuItem>
            <ContextMenuSeparator />
            <ContextMenuItem icon={Trash2} onSelect={() => onDelete(index)} destructive>
              Delete
            </ContextMenuItem>
          </>
        ) : (
          <>
            <ContextMenuItem icon={Copy} onSelect={() => navigator.clipboard.writeText(cmdLabel)}>
              Copy Command
            </ContextMenuItem>
            {onRequeue && 'payload' in item && (
              <ContextMenuItem icon={RotateCcw} onSelect={() => onRequeue(item as TxHistoryItem)}>
                Re-queue
              </ContextMenuItem>
            )}
          </>
        )}
      </ContextMenuContent>
    </ContextMenuRoot>
  )
}
