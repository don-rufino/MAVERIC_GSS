import { useEffect, useState, useCallback, useRef } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { colors } from '@/lib/colors'
import { Kbd } from '@/components/ui/kbd'
import { X, FileText, Database } from 'lucide-react'

interface HelpModalProps {
  open: boolean
  onClose: () => void
}

interface ShortcutRow {
  keys: string
  desc: string
}

const sections: { title: string; items: ShortcutRow[] }[] = [
  {
    title: 'SENDING',
    items: [
      { keys: 'Ctrl+K', desc: 'Search' },
      { keys: 'Ctrl+S', desc: 'Send all queued commands' },
      { keys: 'Ctrl+C / Esc', desc: 'Abort current send' },
      { keys: 'Enter', desc: 'Confirm dialogs' },
    ],
  },
  {
    title: 'QUEUE MANAGEMENT',
    items: [
      { keys: 'Drag', desc: 'Reorder queue items' },
      { keys: 'Click row', desc: 'Select / deselect item' },
      { keys: 'Right-click', desc: 'Context menu (guard, dup, move, del)' },
      { keys: 'G', desc: 'Toggle guard on focused item' },
      { keys: 'Guard toggle', desc: 'Mark item for confirmation' },
      { keys: 'Delete', desc: 'Remove selected item' },
      { keys: 'Ctrl+Z', desc: 'Undo last queue action' },
      { keys: 'Ctrl+X', desc: 'Clear queue' },
    ],
  },
  {
    title: 'COMMAND INPUT',
    items: [
      { keys: 'CMD [ARGS]', desc: 'Shorthand entry (schema defaults)' },
      { keys: '[SRC] DEST ECHO TYPE CMD [ARGS]', desc: 'Full form' },
      { keys: 'Up / Down', desc: 'Browse command history' },
      { keys: 'Enter', desc: 'Queue the command' },
    ],
  },
  {
    title: 'RX MONITORING',
    items: [
      { keys: 'Click row', desc: 'View packet details' },
      { keys: 'Click again', desc: 'Minimise / return to live' },
      { keys: 'Right-click', desc: 'Context menu (copy hex/cmd/args)' },
      { keys: '↑ / ↓', desc: 'Navigate packets (when list focused)' },
      { keys: 'Space', desc: 'Toggle expand/collapse detail' },
      { keys: 'HEX pill', desc: 'Toggle hex display' },
      { keys: 'UL pill', desc: 'Toggle uplink echo visibility' },
      { keys: 'FRAME pill', desc: 'Toggle frame type column' },
      { keys: 'WRAP pill', desc: 'Toggle frame wrapper headers' },
    ],
  },
]

const springConfig = { type: 'spring' as const, stiffness: 500, damping: 30, mass: 0.8 }
let hasLoadedHelpModal = false

export function HelpModal({ open, onClose }: HelpModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<Element | null>(null)
  const [status, setStatus] = useState<{ mission_name?: string; version: string; schema_path: string; schema_count: number; log_dir: string } | null>(null)
  const animateOnMount = hasLoadedHelpModal

  useEffect(() => {
    hasLoadedHelpModal = true
  }, [])

  useEffect(() => {
    if (open) {
      triggerRef.current = document.activeElement
      fetch('/api/status').then(r => r.json()).then(setStatus).catch(() => {})
    } else if (triggerRef.current && triggerRef.current instanceof HTMLElement) {
      triggerRef.current.focus()
      triggerRef.current = null
    }
  }, [open])

  // Focus trap
  const handleTab = useCallback((e: KeyboardEvent) => {
    if (e.key !== 'Tab' || !panelRef.current) return
    const focusable = panelRef.current.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )
    if (focusable.length === 0) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (e.shiftKey) {
      if (document.activeElement === first) { e.preventDefault(); last.focus() }
    } else {
      if (document.activeElement === last) { e.preventDefault(); first.focus() }
    }
  }, [])

  useEffect(() => {
    if (!open) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' || e.key === '?') { e.preventDefault(); onClose() }
      if (e.key === 'Tab') handleTab(e)
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [open, onClose, handleTab])

  // Auto-focus panel on open
  useEffect(() => {
    if (open && panelRef.current) {
      const btn = panelRef.current.querySelector<HTMLElement>('button')
      btn?.focus()
    }
  }, [open])

  const missionName = status?.mission_name || 'Mission'

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ backgroundColor: colors.modalBackdrop }}
          initial={animateOnMount ? { opacity: 0 } : false}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          onClick={onClose}
        >
          <motion.div
            ref={panelRef}
            className="w-[640px] max-h-[80vh] overflow-y-auto rounded-lg border p-5 shadow-overlay"
            style={{ backgroundColor: colors.bgPanelRaised, borderColor: colors.borderStrong }}
            initial={animateOnMount ? { scale: 0.95, opacity: 0 } : false}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={springConfig}
            onClick={(e) => e.stopPropagation()}
          >

            <div className="flex items-center justify-between mb-4">
              <span className="text-sm font-bold" style={{ color: colors.label }}>
                Help — {missionName} GSS Web
              </span>
              <button onClick={onClose} className="p-1 rounded hover:bg-white/5">
                <X className="size-4" style={{ color: colors.dim }} />
              </button>
            </div>

            {/* Shortcuts grid */}
            <div className="grid grid-cols-2 gap-4">
              {sections.map((section) => (
                <div key={section.title}>
                  <div className="text-xs font-bold uppercase tracking-wider mb-2" style={{ color: colors.label }}>
                    {section.title}
                  </div>
                  <div className="flex flex-col gap-1.5">
                    {section.items.map((item) => (
                      <div key={item.keys} className="flex items-start gap-2">
                        <Kbd className="shrink-0">{item.keys}</Kbd>
                        <span className="text-xs font-light pt-0.5" style={{ color: colors.dim }}>{item.desc}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {/* Session info */}
            {status && (
              <>
                <div className="mt-4 pt-3 border-t" style={{ borderColor: colors.borderSubtle }}>
                  <div className="text-xs font-bold uppercase tracking-wider mb-2" style={{ color: colors.label }}>SESSION</div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                    <div className="flex items-center gap-1.5">
                      <Database className="size-3" style={{ color: colors.dim }} />
                      <span style={{ color: colors.dim }}>Version</span>
                      <span className="ml-auto font-mono" style={{ color: colors.value }}>{status.version}</span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <FileText className="size-3" style={{ color: colors.dim }} />
                      <span style={{ color: colors.dim }}>Schema</span>
                      <span className="ml-auto font-mono" style={{ color: colors.value }}>{(status.schema_path || '').split('/').pop()}</span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <span className="size-3" />
                      <span style={{ color: colors.dim }}>Commands</span>
                      <span className="ml-auto font-mono" style={{ color: colors.value }}>{status.schema_count}</span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <span className="size-3" />
                      <span style={{ color: colors.dim }}>Log Dir</span>
                      <span className="ml-auto font-mono" style={{ color: colors.value }}>{status.log_dir}</span>
                    </div>
                  </div>
                </div>
              </>
            )}

            <div className="mt-4 pt-3 border-t text-xs text-center" style={{ color: colors.dim, borderColor: colors.borderSubtle }}>
              Press <Kbd>?</Kbd> or <Kbd>Esc</Kbd> to close
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
