import { useEffect, useRef } from 'react'
import { useShortcuts } from '@/hooks/useShortcuts'
import { motion, AnimatePresence } from 'framer-motion'
import { colors } from '@/lib/colors'
import {
  Command, CommandInput, CommandList, CommandEmpty,
  CommandGroup, CommandItem, CommandShortcut,
} from '@/components/ui/command'
import { strictFilter } from '@/lib/cmdkFilter'
import {
  Send, Trash2, Undo, Binary, Radio, Shield,
  Settings, FileText, HelpCircle, Eye, Plus, Tag,
  Database,
} from 'lucide-react'
import type { NavigationTabDef } from '@/lib/navigation'

export interface CommandPaletteActions {
  toggleHex: () => void
  toggleUplink: () => void
  toggleFrame: () => void
  toggleWrapper: () => void
  openConfig: () => void
  openLogs: () => void
  openHelp: () => void
  newSession: () => void
  tagSession: () => void
  confirmSend?: () => void
  confirmClear?: () => void
  undoLast?: () => void
  abortSend?: () => void
}

export interface CommandPaletteProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  navigationTabs: NavigationTabDef[]
  onNavigate: (tabId: string, subId?: string) => void
  actions: CommandPaletteActions
}

const springConfig = { type: 'spring' as const, stiffness: 500, damping: 30, mass: 0.8 }
let hasLoadedCommandPalette = false

export function CommandPalette({ open, onOpenChange, navigationTabs, onNavigate, actions }: CommandPaletteProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const animateOnMount = hasLoadedCommandPalette

  useEffect(() => {
    hasLoadedCommandPalette = true
  }, [])

  function run(fn: () => void) {
    fn()
    onOpenChange(false)
  }

  // Focus input when opened
  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 50)
  }, [open])

  // Escape closes
  useShortcuts(
    [{ key: 'Escape', action: () => onOpenChange(false) }],
    open,
  )

  return (
    <AnimatePresence initial={false}>
      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center pt-[20vh]" onClick={() => onOpenChange(false)}>
          <motion.div
            className="absolute inset-0 frosted-backdrop"
            style={{ backgroundColor: colors.modalBackdrop }}
            initial={animateOnMount ? { opacity: 0 } : false}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
          />
          <motion.div
            className="relative z-10 w-[480px] shadow-overlay rounded-xl overflow-hidden border"
            style={{ backgroundColor: colors.bgPanelRaised, borderColor: colors.borderStrong }}
            onClick={(e) => e.stopPropagation()}
            initial={animateOnMount ? { opacity: 0, scale: 0.95, y: -10 } : false}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: -10 }}
            transition={springConfig}
          >
            <Command className="bg-transparent" filter={strictFilter}>
              <CommandInput ref={inputRef} placeholder="Search..." />
              <CommandList>
                <CommandEmpty>No results found.</CommandEmpty>

                {(actions.confirmSend || actions.confirmClear || actions.undoLast || actions.abortSend) && (
                  <CommandGroup heading="Uplink">
                    {actions.confirmSend && (
                      <CommandItem onSelect={() => run(actions.confirmSend!)}>
                        <Send className="size-4" />
                        <span>Send All...</span>
                        <CommandShortcut>Ctrl+S</CommandShortcut>
                      </CommandItem>
                    )}
                    {actions.confirmClear && (
                      <CommandItem onSelect={() => run(actions.confirmClear!)}>
                        <Trash2 className="size-4" />
                        <span>Clear Queue...</span>
                        <CommandShortcut>Ctrl+X</CommandShortcut>
                      </CommandItem>
                    )}
                    {actions.undoLast && (
                      <CommandItem onSelect={() => run(actions.undoLast!)}>
                        <Undo className="size-4" />
                        <span>Undo Last</span>
                        <CommandShortcut>Ctrl+Z</CommandShortcut>
                      </CommandItem>
                    )}
                    {actions.abortSend && (
                      <CommandItem onSelect={() => run(actions.abortSend!)}>
                        <Shield className="size-4" />
                        <span>Abort Send</span>
                        <CommandShortcut>Esc</CommandShortcut>
                      </CommandItem>
                    )}
                  </CommandGroup>
                )}

                <CommandGroup heading="Downlink Display">
                  <CommandItem onSelect={() => run(actions.toggleHex)}>
                    <Binary className="size-4" />
                    <span>Toggle Hex Display</span>
                  </CommandItem>
                  <CommandItem onSelect={() => run(actions.toggleUplink)}>
                    <Eye className="size-4" />
                    <span>Toggle Uplink Echoes</span>
                  </CommandItem>
                  <CommandItem onSelect={() => run(actions.toggleFrame)}>
                    <Radio className="size-4" />
                    <span>Toggle Frame Column</span>
                  </CommandItem>
                  <CommandItem onSelect={() => run(actions.toggleWrapper)}>
                    <Shield className="size-4" />
                    <span>Toggle frame wrapper</span>
                  </CommandItem>
                </CommandGroup>

                <CommandGroup heading="Logging">
                  <CommandItem onSelect={() => run(actions.newSession)}>
                    <Plus className="size-4" />
                    <span>New Log Session</span>
                  </CommandItem>
                  <CommandItem onSelect={() => run(actions.tagSession)}>
                    <Tag className="size-4" />
                    <span>Tag Current Session</span>
                  </CommandItem>
                </CommandGroup>

                <CommandGroup heading="Panels">
                  <CommandItem onSelect={() => run(actions.openConfig)}>
                    <Settings className="size-4" />
                    <span>Open Configuration</span>
                  </CommandItem>
                  <CommandItem onSelect={() => run(actions.openLogs)}>
                    <FileText className="size-4" />
                    <span>Open Log Viewer</span>
                  </CommandItem>
                  <CommandItem onSelect={() => run(actions.openHelp)}>
                    <HelpCircle className="size-4" />
                    <span>Help & Shortcuts</span>
                    <CommandShortcut>?</CommandShortcut>
                  </CommandItem>
                </CommandGroup>

                <CommandGroup heading="Navigation">
                  {navigationTabs.map(t => (
                    <CommandItem
                      key={t.id}
                      value={t.name}
                      keywords={['go to', t.id]}
                      onSelect={() => run(() => onNavigate(t.id))}
                    >
                      <t.icon className="size-4" />
                      <span>Go to {t.name}</span>
                    </CommandItem>
                  ))}
                  {navigationTabs.flatMap(t =>
                    t.kind === 'plugin' && t.plugin.subroutes
                      ? t.plugin.subroutes.map(sub => (
                          <CommandItem
                            key={`${t.id}:${sub}`}
                            value={`${t.name} ${sub}`}
                            keywords={['go to', t.id, sub]}
                            onSelect={() => run(() => onNavigate(t.id, sub))}
                          >
                            <Database className="size-4" />
                            <span>Go to {t.name} {sub.charAt(0).toUpperCase() + sub.slice(1)}</span>
                          </CommandItem>
                        ))
                      : []
                  )}
                </CommandGroup>
              </CommandList>
            </Command>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
