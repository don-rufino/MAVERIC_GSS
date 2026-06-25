import { useState, useEffect, useCallback, useMemo, lazy, Suspense } from 'react'
import { useShortcuts, type Shortcut } from '@/hooks/useShortcuts'
import { SessionProvider } from '@/state/SessionProvider'
import { useSessionContext, useConfig } from '@/state/sessionHooks'
import { TxProvider } from '@/state/TxProvider'
import { useTx } from '@/state/txHooks'
import { RxProvider } from '@/state/RxProvider'
import { RadioProvider } from '@/state/RadioProvider'
import { TrackingProvider } from '@/state/TrackingProvider'
import { ParametersProvider } from '@/state/ParametersProvider'
import { useRxDisplayToggles } from '@/state/rxHooks'
import { colors } from '@/lib/colors'
import { GlobalHeader, RenameSessionDialog } from '@/components/layout/GlobalHeader'
import { Toaster } from '@/components/ui/sonner'
import {
  MainDashboard,
  RxCrcToastSentinel,
  ConfigModalSkeleton,
  LogViewerSkeleton,
  HelpModalSkeleton,
  CommandPaletteSkeleton,
} from '@/components/MainDashboard'
import { AlarmStrip } from '@/components/shared/overlays/AlarmStrip'
import { getPluginPages, type PluginPageDef } from '@/plugins/registry'
import { MissionProviders } from '@/plugins/missionRuntime'
import { usePreflight } from '@/hooks/usePreflight'
import { PreflightScreen } from '@/components/shared/preflight/PreflightScreen'
import { TabViewport } from '@/components/layout/TabViewport'
import { buildNavigationTabs } from '@/lib/navigation'
import { KeyboardHintBar } from '@/components/layout/KeyboardHintBar'
import { isInputFocused } from '@/lib/utils'
import { authFetch } from '@/lib/auth'
import type { CommandPaletteActions } from '@/components/shared/overlays/CommandPalette'

const ConfigModal = lazy(() => import('@/components/config/ConfigModal').then((m) => ({ default: m.ConfigModal })))
const LogViewer = lazy(() => import('@/components/logs/LogViewer').then((m) => ({ default: m.LogViewer })))
const HelpModal = lazy(() => import('@/components/shared/dialogs/HelpModal').then((m) => ({ default: m.HelpModal })))
const CommandPalette = lazy(() => import('@/components/shared/overlays/CommandPalette').then((m) => ({ default: m.CommandPalette })))
const RadioPage = lazy(() => import('@/components/radio/RadioPage').then((m) => ({ default: m.RadioPage })))

/** Read the ?page= param for plugin page routing */
function getPageMode(): string | null {
  const params = new URLSearchParams(window.location.search)
  return params.get('page') || null
}

export default function App() {
  return (
    <SessionProvider>
      <TxProvider>
        <RxProvider>
          <ParametersProvider>
            <RadioProvider>
              <TrackingProvider>
                <MissionProviders>
                  <AppShell />
                  <PreflightOverlay />
                </MissionProviders>
              </TrackingProvider>
            </RadioProvider>
          </ParametersProvider>
        </RxProvider>
      </TxProvider>
    </SessionProvider>
  )
}

/** Main app shell — lives inside Session/Tx/Rx providers */
function AppShell() {
  const { config, setConfig } = useConfig()
  const [page, setPage] = useState<string | null>(() => getPageMode())
  const [plugins, setPlugins] = useState<PluginPageDef[]>([])

  // Shell modal state (lifted from old MainDashboard)
  const [showLogs, setShowLogs] = useState(false)
  const [showConfig, setShowConfig] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [showCommand, setShowCommand] = useState(false)
  const [confirmSendSignal, setConfirmSendSignal] = useState(0)
  const [confirmClearSignal, setConfirmClearSignal] = useState(0)

  const tx = useTx()
  const session = useSessionContext()
  const rxToggles = useRxDisplayToggles()

  // Load plugin pages once mission is known
  useEffect(() => {
    const missionId = config?.mission?.id
    if (!missionId) return
    getPluginPages(missionId).then(setPlugins)
  }, [config?.mission?.id])

  const navigateTo = useCallback((target: string | null, sub?: string) => {
    const url = new URL(window.location.href)
    if (target && target !== '__dashboard__') {
      url.searchParams.set('page', target)
    } else {
      url.searchParams.delete('page')
    }
    if (sub) {
      url.searchParams.set('tab', sub)
    } else {
      url.searchParams.delete('tab')
    }
    window.history.pushState({}, '', url.toString())
    setPage(target === '__dashboard__' ? null : target)
    window.dispatchEvent(new Event('gss:nav'))
  }, [])

  // Browser back/forward
  useEffect(() => {
    const onPopState = () => setPage(getPageMode())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  useEffect(() => {
    const missionName = config?.mission.name ?? 'Mission'
    document.title = `${missionName} GSS`
  }, [config?.mission.name])

  const version = config?.platform.general.version ?? '...'
  const missionName = config?.mission.name ?? 'Mission'

  // Derived state
  const activeTabId = page ?? '__dashboard__'
  const navigationTabs = useMemo(() => buildNavigationTabs(plugins), [plugins])

  // Build palette actions — dashboard-scoped entries are conditional
  const paletteActions = useMemo<CommandPaletteActions>(() => {
    const base: CommandPaletteActions = {
      toggleHex: rxToggles.toggleHex,
      toggleFrame: rxToggles.toggleFrame,
      toggleWrapper: rxToggles.toggleWrapper,
      toggleUplink: rxToggles.toggleUplink,
      openConfig: () => setShowConfig(true),
      openLogs: () => setShowLogs(true),
      openHelp: () => setShowHelp(true),
      newSession: () => session.setOpenNewSession(true),
      tagSession: () => session.setOpenRename(true),
    }
    if (activeTabId === '__dashboard__') {
      return {
        ...base,
        confirmSend: () => setConfirmSendSignal(n => n + 1),
        confirmClear: () => setConfirmClearSignal(n => n + 1),
        undoLast: tx.undoLast,
        abortSend: tx.abortSend,
      }
    }
    return base
  }, [activeTabId, rxToggles, tx, session])

  // Shell-global shortcuts
  const shellShortcuts = useMemo<Shortcut[]>(() => [
    { key: 'k', ctrl: true, action: () => setShowCommand(v => !v) },
    { key: '?', action: () => setShowHelp(v => !v), when: () => !isInputFocused() },
    { key: 'Escape', action: () => setShowConfig(false), when: () => showConfig },
    { key: 'Escape', action: () => setShowLogs(false), when: () => showLogs },
    { key: 'Escape', action: () => setShowHelp(false), when: () => showHelp },
  ], [showConfig, showLogs, showHelp])

  useShortcuts(shellShortcuts)

  return (
    <div className="flex flex-col h-full" style={{ backgroundColor: colors.bgApp }}>
      <GlobalHeader
        missionName={missionName}
        version={version}
        tabs={navigationTabs}
        activeTabId={activeTabId}
        onTabClick={(id) => navigateTo(id === '__dashboard__' ? null : id)}
        onLogsClick={() => setShowLogs(v => !v)}
        onConfigClick={() => setShowConfig(v => !v)}
        onHelpClick={() => setShowHelp(v => !v)}
        session={session}
      />
      <RenameSessionDialog session={session} />
      <RxCrcToastSentinel />
      <AlarmStrip />
      <TabViewport
        plugins={plugins}
        activeId={activeTabId}
        renderDashboard={() => (
          <MainDashboard
            config={config}
            confirmSendSignal={confirmSendSignal}
            confirmClearSignal={confirmClearSignal}
          />
        )}
        renderRadio={() => (
          <Suspense fallback={
            <div className="flex-1 flex items-center justify-center">
              <span className="text-[11px]" style={{ color: colors.textMuted }}>Loading radio...</span>
            </div>
          }>
            <RadioPage />
          </Suspense>
        )}
      />
      {/* Lazy modals */}
      {showConfig && (
        <Suspense fallback={<ConfigModalSkeleton />}>
          <ConfigModal open={showConfig} onClose={() => { setShowConfig(false); fetch('/api/config').then(r => r.json()).then(setConfig) }} />
        </Suspense>
      )}
      {showLogs && (
        <Suspense fallback={<LogViewerSkeleton />}>
          <LogViewer open={showLogs} onClose={() => setShowLogs(false)} />
        </Suspense>
      )}
      {showHelp && (
        <Suspense fallback={<HelpModalSkeleton />}>
          <HelpModal open={showHelp} onClose={() => setShowHelp(false)} />
        </Suspense>
      )}
      {showCommand && (
        <Suspense fallback={<CommandPaletteSkeleton />}>
          <CommandPalette
            open={showCommand}
            onOpenChange={setShowCommand}
            navigationTabs={navigationTabs}
            onNavigate={(id, sub) => { navigateTo(id === '__dashboard__' ? null : id, sub) }}
            actions={paletteActions}
          />
        </Suspense>
      )}
      <KeyboardHintBar activeTabId={activeTabId} anyShellModalOpen={showLogs || showConfig || showHelp} />
      <Toaster position="top-center" />
    </div>
  )
}

/** Frosted overlay that blurs the dashboard during preflight, then lifts to reveal it */
function PreflightOverlay() {
  const preflight = usePreflight()
  const { config } = useConfig()
  const version = config?.platform.general.version
  const buildSha = config?.platform.general.build_sha
  const [dismissing, setDismissing] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const [identity, setIdentity] = useState<{ operator: string; station: string } | null>(null)
  useEffect(() => {
    authFetch('/api/identity')
      .then((r) => r.json())
      .then((data: { operator?: string; station?: string }) => {
        if (data.operator && data.station) {
          setIdentity({ operator: data.operator, station: data.station })
        }
      })
      .catch(() => {})
  }, [])

  // No auto-dismiss — the overlay stays visible until the user presses LAUNCH.
  // After LAUNCH triggers dismissing=true, unmount after the unblur animation (800ms).
  useEffect(() => {
    if (dismissing) {
      const t = setTimeout(() => setDismissed(true), 850)
      return () => clearTimeout(t)
    }
  }, [dismissing])

  if (dismissed) return null

  return (
    <PreflightScreen
      checks={preflight.checks}
      summary={preflight.summary}
      connected={preflight.connected}
      dismissing={dismissing}
      onContinue={() => {
        preflight.signalLaunched()
        setDismissing(true)
      }}
      onRerun={preflight.rerun}
      updateState={preflight.updateState}
      updatePhases={preflight.updatePhases}
      onShowConfirm={preflight.showConfirm}
      onCancelConfirm={preflight.cancelConfirm}
      onApplyUpdate={preflight.applyUpdate}
      onReloadPage={preflight.reloadPage}
      version={version}
      buildSha={buildSha}
      operator={identity?.operator}
      station={identity?.station}
    />
  )
}
