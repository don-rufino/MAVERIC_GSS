# MAVERIC GSS Web Console

This directory contains the React/Vite operator console served by the FastAPI
runtime in `mav_gss_lib/server/`. It is not deployed as a standalone app:
`dist/` is committed and mounted directly by `python3 MAV_WEB.py`, while Vite
development proxies `/api` and `/ws` to the backend on port 8080.

## What lives here

- RX packet monitoring, detail inspection, packet replay, and live parameter cache views.
- TX command builder, drag-to-reorder queue, delays, notes, checkpoints, guard prompts, sent history, and verifier status.
- GNU Radio process-control tab for `platform.radio.script`, with start/stop/restart, stdout/stderr tailing, and the embedded Doppler engagement controls.
- Runtime config editor, log browser, command palette, help modal, alarm strip, status toasts, and session controls.
- Preflight and updater screens shown before the operator launches into the console.
- Mission plugin pages and providers, currently MAVERIC Files, GNC, and EPS.

## Tech stack

Verified against `package.json`:

- React 19, Vite 8, TypeScript 5.9
- Tailwind CSS v4 through `@tailwindcss/vite`, with shadcn/ui primitives in `src/components/ui`
- `@base-ui/react` and `@radix-ui/react-context-menu` for unstyled primitives
- `@dnd-kit/*` for TX queue drag-and-drop
- `react-resizable-panels` for split panes and `react-virtuoso` for virtualized lists
- `cmdk` for the Ctrl+K command palette
- `sonner` for toasts and `lucide-react` for iconography
- `react-day-picker` for log filters
- `three`, `d3-geo`, `topojson-client`, and `satellite.js` for visualization surfaces
- `@fontsource-variable/inter` for UI text and `@fontsource-variable/jetbrains-mono` for data rows

The frontend version in `package.json` is the repository's single source of
truth. The backend reads it through `mav_gss_lib/config.py::_read_version()`.

## Build

```bash
cd mav_gss_lib/web
npm install
npm run dev            # Vite HMR, proxies /api and /ws to :8080
npm run build          # tsc -b && vite build -> emits dist/
npm run lint
```

`npm run build` runs TypeScript first and then emits `dist/`. Because the
Python server serves `dist/`, every UI source change under `src/` should be
paired with a refreshed production build. `node_modules/` is untracked.

The backend must be running during `npm run dev`; otherwise API and WebSocket
proxy calls have nowhere to land.

## Directory layout

```text
src/
  App.tsx                  Provider tree, shell routing, lazy modals, preflight overlay
  main.tsx                 React root and CSS import
  index.css                Tailwind directives, tokens, fonts, global console styling
  components/
    MainDashboard.tsx      RX/TX split-pane dashboard and shell skeletons
    ConfigSidebar.tsx      Runtime config editor
    radio/                 RadioPage: GNU Radio process supervisor UI
    rx/                    RxPanel, packet list/detail, session banner, blackout pill
    tx/                    TxPanel, TxQueue, queue rows, delay/note/checkpoint rows,
                           import dialog, verifier tick/detail blocks, command input
    logs/                  Log session browser
    layout/                GlobalHeader, TabViewport, TabStrip, SplitPane, hint bar
    shared/                atoms, dialogs, overlays, preflight, rendering, visualization
    ui/                    shadcn/ui primitives
  hooks/
    useRxSocket.ts         /ws/rx consumer with 50 ms buffered flushes
    rxSocketState.ts       RX packet buffer, batch handling, per-event parameters
    useTxSocket.ts         /ws/tx consumer and queue/action helpers
    useLogQuery.ts         Paginated log and parameter fetching
    useAlarms.ts           Alarm stream state
    usePreflight.ts        Preflight/update stream state
    useContainerFreshness.ts, useReceivingDetection.ts, useShortcuts.ts, ...
  state/
    SessionProvider.tsx    Session identity and rename/new-session state
    TxProvider.tsx         TX socket, queue, history, verification map
    RxProvider.tsx         RX socket, status, packet stats, display toggles
    ParametersProvider.tsx /api/parameters snapshot plus live freshness/cache updates
    RadioProvider.tsx      /ws/radio status + log fan-out for the Radio tab
    TrackingProvider.tsx   /ws/tracking + /api/tracking/* state for the Doppler section
    *Contexts.ts           Context objects split from provider components
    *Hooks.ts              Provider selectors and convenience hooks
  lib/
    auth.ts                API-token fetch helper
    ws.ts                  WebSocket factory
    types.ts               Shared RX/TX/config/plugin/alarm types
    rxPacket.ts            RX row/detail adapters
    txDetail.ts            TX detail adapters
    columns.ts             Declarative RX/TX column rendering helpers
    navigation.ts          Dashboard, Radio, and mission-plugin tab definitions
    colors.ts              Semantic console palette
    cmdkFilter.ts          Command-palette filter predicate
  plugins/
    registry.ts            Mission plugin discovery via import.meta.glob
    missionRuntime.tsx     Mission provider discovery and composition
    maveric/
      TxBuilder.tsx        MAVERIC command builder
      plugins.ts           Files, GNC, EPS page manifest
      providers.ts         Mission provider manifest
      preview/             Files page (DownlinkPreview.tsx — image / AII / MAG)
      files/               Files-page chunk hooks, adapters, and inline previews
      gnc/                 GNC dashboard, registers, 3D viewer, shared widgets
      eps/                 EPS dashboard, live cards, field panes

public/                    Static assets copied into dist/
dist/                      Production build served by FastAPI (committed)
```

Providers live under `src/state/`; `src/hooks/` contains reusable hooks and
socket state helpers. Keep that split when adding new app-wide state.

## Provider tree

`App.tsx` wraps the console in this order:

1. `SessionProvider`
2. `TxProvider`
3. `RxProvider`
4. `ParametersProvider`
5. `RadioProvider`
6. `TrackingProvider`
7. `MissionProviders`
8. `AppShell` plus `PreflightOverlay`

`AppShell` renders `GlobalHeader`, `RenameSessionDialog`, `RxCrcToastSentinel`,
`AlarmStrip`, `TabViewport`, lazy-loaded `ConfigSidebar`, `LogViewer`,
`HelpModal`, `CommandPalette`, `KeyboardHintBar`, and `Toaster`.

`TabViewport` keeps the main dashboard and Radio page mounted but hidden when
inactive. Mission plugin pages render when active; pages marked `keepAlive`
(currently GNC) also stay mounted across tab switches.

## URL modes

Routing is query-param based and handled in `App.tsx`:

| URL | Renders |
|---|---|
| `/` | Dashboard tab (`MainDashboard`) |
| `/?page=__radio__` | Radio tab (`components/radio/RadioPage`, includes `DopplerSection`) |
| `/?page=files` | MAVERIC Files plugin (image / AII / MAG downlink workbench) |
| `/?page=gnc` | MAVERIC GNC plugin |
| `/?page=gnc&tab=registers` | GNC registers sub-tab |
| `/?page=eps` | MAVERIC EPS plugin |

Unknown `page` values show a small not-found state. Plugin sub-tabs use `tab=`.
There is no current `?panel=` pop-out mode in `App.tsx`.

## Backend contract

React renders normalized backend models. It does not parse packet wire formats
or frame commands; those responsibilities belong to the active mission package
and the Python platform.

WebSocket endpoints:

| Path | Purpose |
|---|---|
| `/ws/rx` | RX packets, RX batches, replay events, parameter snapshots, blackout messages, mission plugin events |
| `/ws/tx` | Queue updates, history, send progress, guard/checkpoint prompts, verifier restore/update |
| `/ws/radio` | GNU Radio process status and captured stdout/stderr |
| `/ws/tracking` | 1 Hz Doppler ticks, engagement-mode transitions, station/pass status |
| `/ws/session` | Session identity, new-session, rename, and traffic-status events |
| `/ws/preflight` | Preflight checks and updater progress |
| `/ws/alarms` | Alarm snapshot, changes, removals, and ack flow |

HTTP endpoints used by the UI:

| Path | Purpose |
|---|---|
| `GET /api/status` | Runtime health, version, ZMQ status, auth token, active session log |
| `GET /api/selfcheck` | Lightweight environment self-check |
| `GET /api/config`, `PUT /api/config` | Native split config read/write |
| `GET /api/schema` | Active mission command schema |
| `GET /api/rx-columns`, `GET /api/tx-columns` | Declarative column definitions from `mission.yml` |
| `GET /api/tx/capabilities` | Mission TX capability flags |
| `GET /api/rx/packets/{event_id}` | In-memory decoded RX packet detail by event id |
| `GET /api/radio/status`, `GET /api/radio/logs` | Radio supervisor snapshots |
| `POST /api/radio/start`, `POST /api/radio/stop`, `POST /api/radio/restart` | Radio supervisor control actions |
| `GET /api/tracking/config` | Active tracking config (station catalog, TLE, frequencies, display) |
| `GET /api/tracking/state` | Live tracking snapshot at `at_ms` (defaults to now), with up to 20 upcoming passes |
| `GET /api/tracking/passes` | Pass-prediction window (`from_ms`, `count` 1-30) |
| `GET /api/tracking/pass/{id}` | Detail for a specific pass id |
| `GET /api/tracking/doppler` | Doppler shift at `at_ms` (defaults to now) |
| `POST /api/tracking/doppler/connection/{connect\|disconnect}` | Engage / disengage the live Doppler sink |
| `GET /api/logs`, `GET /api/logs/{session_id}` | Log session list and RX/TX record pages |
| `GET /api/logs/{session_id}/parameters` | Parameter rows for one log session |
| `GET /api/parameters`, `DELETE /api/parameters/group/{group}` | Parameter spec/freshness and group clear |
| `GET /api/session`, `POST /api/session/new`, `PATCH /api/session` | Session read/new/rename |
| `GET /api/identity` | Operator, host, and station identity |
| `GET /api/import-files`, `GET /api/import/{filename}/preview`, `POST /api/import/{filename}` | Queue import |
| `POST /api/export-queue`, `POST /api/tx/clear-sent` | Queue export and sent-history clear |

Mission routers add their own endpoints. MAVERIC currently mounts
`/api/plugins/maveric/identity` and `/api/plugins/files/*`.

## Development guidance

- Keep protocol truth in the backend mission package. React should render
  `mission.facts`, `parameters`, and declarative columns.
- Add mission-specific pages under `src/plugins/<mission>/`; `registry.ts`
  discovers plugin manifests by convention.
- Add mission-specific providers in `src/plugins/<mission>/providers.ts`;
  `MissionProviders` composes them after `ParametersProvider`.
- Keep new app-wide providers in `src/state/` and plain reusable hooks in
  `src/hooks/`.
- Follow the black mission-console palette in `src/lib/colors.ts`: sparse
  semantic accents, no large colored panel fills, and no new sub-11px text
  without a deliberate reason.
