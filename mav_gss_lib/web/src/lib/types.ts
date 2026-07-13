// ---- RX ----

export interface RxPacket {
  event_id?: string
  num: number
  time?: string
  time_utc?: string
  received_at_ms?: number
  frame: string
  size: number
  raw_hex: string
  mission?: MissionFacts
  warnings: string[]
  is_echo: boolean
  is_dup: boolean
  is_unknown: boolean
  flags?: RxFlags
  _rendering?: RenderingData
}

export interface MissionFacts {
  id: string
  cmd_id?: string
  facts?: Record<string, unknown>
}

export interface RxFlags {
  duplicate?: boolean
  unknown?: boolean
  uplink_echo?: boolean
  integrity_ok?: boolean | null
}

export interface ParamUpdate {
  name: string
  value: unknown
  ts_ms: number
  unit?: string
  display_only?: boolean
}

// ---- Rendering Slots ----

// Declarative column def — superset shape covering both platform-shell
// columns (RX num/time/frame/flags/size — `kind` and `path` omitted) and
// mission-authored columns from `mission.yml::ui.{rx,tx}_columns`
// (`path` for `kind: value`, no path for `kind: verifiers`).
export interface ColumnDef {
  id: string
  label: string
  /** Fixed column width in CSS pixels. Applied as inline style by
   *  `columnWidthStyle` — CSS-framework-agnostic, no build-time class
   *  allowlist required. Omit for content-width or `flex: true` columns. */
  width?: number
  align?: 'left' | 'right'
  flex?: boolean
  toggle?: string
  path?: string
  kind?: 'value' | 'verifiers' | 'cmd_id'
  badge?: boolean
  truncate?: boolean
  value_icons?: Record<string, string>
  default_icon?: string
  hide_if_all?: unknown[]
}

export interface RenderingFlag {
  tag: string
  tone: string
}

export interface RenderingMeta {
  opacity?: number
}

export interface RenderCell {
  value: string | number | boolean | null | RenderingFlag[]
  tone?: string | null
  badge?: boolean
  tooltip?: string | null
  monospace?: boolean
  tabular?: boolean       // tabular-nums for fixed-width numbers
  suffix?: string         // appended verbatim to the rendered text (e.g. "B" for sizes)
}

export interface BlockField {
  name: string
  value: string
}

export interface DetailBlock {
  kind: string
  label: string
  fields: BlockField[]
}

export interface IntegrityBlock {
  kind: string
  label: string
  scope: string
  ok: boolean | null
  received?: string | null
  computed?: string | null
}

export interface RenderingData {
  row: Record<string, RenderCell>
  meta?: RenderingMeta
  detail_blocks: DetailBlock[]
  protocol_blocks: DetailBlock[]
  integrity_blocks: IntegrityBlock[]
}

export interface RxStatus {
  zmq: string
  pkt_rate: number
  silence_s: number
}

// ---- TX ----
// TX queue / history items mirror the RX shape: `mission: {id, facts}` is
// the mission-owned opaque dict consumed by declarative TX columns and the
// detail panel. `parameters` is the typed-args list used by the parameter
// blocks in the detail panel. There is no rich `display` object — all
// presentation flows through declarative `mission.yml::ui.tx_columns`.

export interface ColumnDefs {
  rx: ColumnDef[]
  tx: ColumnDef[]
}

export interface TxQueueCmd {
  type: 'mission_cmd'
  num: number
  cmd_id: string
  mission?: MissionFacts
  parameters?: ParamUpdate[]
  guard: boolean
  size: number
  raw_hex: string
  payload: Record<string, unknown>
  // Backend stamps this on the still-queued item right after register so the
  // tick strip can render mid-send. Absent on pending (not-yet-sent) rows.
  event_id?: string
}

export interface TxQueueDelay {
  type: 'delay'
  delay_ms: number
}

export interface TxQueueNote {
  type: 'note'
  text: string
}

export interface TxQueueCheckpoint {
  type: 'checkpoint'
  text: string
}

export type TxQueueItem = TxQueueCmd | TxQueueDelay | TxQueueNote | TxQueueCheckpoint

export interface TxQueueSummary {
  cmds: number
  guards: number
  checkpoints?: number
  est_time_s: number
}

export interface TxHistoryItem {
  n: number
  ts: string
  type: 'mission_cmd'
  cmd_id: string
  mission?: MissionFacts
  parameters?: ParamUpdate[]
  payload: Record<string, unknown>
  size: number
  raw_hex: string
  wire_hex: string
  // Join key to the verification Map. Stamped by backend `_record_sent`;
  // the same id is used as `CommandInstance.cmd_event_id` when the instance
  // is registered. Optional because older persisted rows may not have it.
  event_id?: string
}

export interface SendProgress {
  sent: number
  total: number
  current: string
  waiting?: boolean
}

export interface GuardConfirm {
  index: number
  cmd_id: string
  mission?: MissionFacts
  kind?: 'command' | 'checkpoint'
  text?: string
}

// ---- Unified TX timeline ----
// Per-row lifecycle. Only `pending` / `sending` / `complete` are reached
// today; `released` / `accepted` / `failed` / `timed_out` are reserved for
// the planned command-verification feature. Adding a state here does not
// require any layout change.
export type TxRowStatus =
  | 'pending'
  | 'sending'
  | 'released'
  | 'accepted'
  | 'complete'
  | 'failed'
  | 'timed_out'

export interface TxDisplayItem {
  // Stable DOM/dnd-kit key. Pending: `q-<counter>`. Sent: `h-<TxHistoryItem.n>`.
  uid: string
  status: TxRowStatus
  source: 'queue' | 'history'
  // Queue index for queue items — used for delete/guard/duplicate/reorder.
  queueIndex?: number
  historyN?: number
  item: TxQueueItem | TxHistoryItem
}

// Optimistic drag-reorder override: short-lived local reshuffle of the
// pending segment, applied on top of the backend queue until the backend
// echoes the reorder back. Keeps drag-and-drop snappy.
export interface PendingReorderOverride {
  // Array of queue indices in the desired new order (e.g. [2, 0, 1, 3]).
  order: number[]
  // Monotonic token bumped each time the override is installed. Cleared
  // when `queue` length changes (i.e. backend echoed, or queue shrank
  // from a send).
  token: number
}

export interface TxCapabilities {
  mission_commands: boolean
}

export interface MissionBuilderProps {
  onQueue: (payload: Record<string, unknown>) => void
  onClose: () => void
  disabled?: boolean
}

// Single TX argument shape exposed by GET /api/schema.
// Mirrors `TxArgSchema` from
// mav_gss_lib/platform/contract/commands.py.
//
// `name` and `type` are required by the runtime; everything else is
// type-driven metadata. Keep TS optionality (`?`) in sync with the
// Python `NotRequired[...]` markers.
export interface TxArgSchema {
  name: string
  type: string
  important?: boolean
  optional?: boolean
  description?: string
  valid_range?: [number, number] | null
  valid_values?: Array<string | number> | null
}

// Mission-AGNOSTIC command-schema item. Mirrors the Python
// `CommandSchemaItem` TypedDict — only platform fields. Mission
// plugins extend this for their own routing surfaces (see
// `plugins/maveric/types.ts::MavericCommandSchemaItem`, which adds
// dest/echo/ptype/nodes).
//
// One source of truth: when this interface changes, the Python
// CommandSchemaItem MUST update in the same change, and vice versa.
// The Python contract test
// (tests/test_command_schema_contract.py) catches drift on the
// backend; tsc + the TxArgRow tests catch drift on the frontend.
export interface CommandSchemaItem {
  tx_args: TxArgSchema[]
  description?: string
  title?: string
  label?: string
  variadic?: boolean
  guard?: boolean
  rx_only?: boolean
  deprecated?: boolean
  verifiers?: Record<string, unknown>
}

// ---- Config ----
// Native split shape returned by GET /api/config and accepted by PUT /api/config.
// Matches the backend WebRuntime primary state: {platform_cfg, mission_id, mission_cfg}.

export interface PlatformTrackingStation {
  id: string
  name: string
  lat_deg: number
  lon_deg: number
  alt_m: number
  min_elevation_deg: number
}

export interface PlatformTrackingConfig {
  enabled: boolean
  selected_station_id: string
  stations: PlatformTrackingStation[]
  tle: {
    source: string
    name: string
    line1: string
    line2: string
    method?: 'manual' | 'fetched' | 'seed'
    fetched_at_ms?: number
  }
  frequencies: {
    rx_hz: number
    tx_hz: number
  }
  display: {
    day_night_map: boolean
  }
  tle_fetch?: {
    identifier: string
    auto_refresh: boolean
    refresh_interval_hours: number
    provider?: 'celestrak' | 'spacetrack'
  }
}

export interface PlatformConfig {
  tx: {
    zmq_addr: string
    frequency: string
    delay_ms: number
    verifiers_enabled: boolean
  }
  rx: {
    zmq_addr: string
    frequency?: string
    tx_blackout_ms?: number
  }
  radio?: {
    enabled?: boolean
    autostart?: boolean
    script?: string
    python?: string
    args?: string[]
    log_lines?: number
    iq_record?: boolean
    iq_raw_record?: boolean
    iq_disk_reserve_gb?: number
    rx_gain?: number
  }
  tracking?: PlatformTrackingConfig
  general: {
    version: string
    build_sha?: string
    log_dir: string
    generated_commands_dir?: string
  }
  stations?: Record<string, string>
}

export interface MissionConfig {
  // Generic mission-config fields the platform shell knows about.
  // Mission-specific fields (e.g. MAVERIC's nodes / ptypes / gs_node)
  // are accessed via the index signature by mission-side plugin code,
  // never typed at this layer.
  mission_name?: string
  rx_title?: string
  tx_title?: string
  [key: string]: unknown
}

export interface GssConfig {
  platform: PlatformConfig
  mission: {
    id: string
    name: string
    config: MissionConfig
  }
}

// Deployable mission entry from GET /api/missions (mission switcher).
export interface MissionInfo {
  id: string
  name: string
}

// ---- Preflight ----

export interface PreflightCheck {
  group: string
  label: string
  status: 'ok' | 'fail' | 'warn' | 'skip'
  fix: string
  detail: string
  meta?: UpdatesCheckMeta | null
}

export interface PreflightSummary {
  total: number
  passed: number
  failed: number
  warnings: number
  skipped?: number
  ready: boolean
}

// ---- Updater ----

export interface UpdatesCheckMeta {
  branch: string
  current_sha: string
  behind_count: number
  commits: { sha: string; subject: string }[]
  missing_pip_deps: string[]
  dirty: boolean
  button: 'apply' | null
  button_disabled: boolean
  button_reason: string | null
  fetch_error?: string
}

export type UpdatePhase = 'git_pull' | 'countdown' | 'restart'

export type UpdateUIState = 'idle' | 'confirming' | 'applying' | 'failed' | 'reloading'

export interface UpdateProgress {
  phase: UpdatePhase
  status: 'pending' | 'running' | 'ok' | 'fail'
  detail?: string
}

// ---- Command verification ----

export type VerifierStage = 'received' | 'accepted' | 'complete' | 'failed'
export type VerifierState = 'pending' | 'passed' | 'failed' | 'window_expired'
export type InstanceStage =
  | 'released' | 'received' | 'accepted' | 'complete' | 'failed' | 'timed_out'

export interface CheckWindow {
  start_ms: number
  stop_ms: number
}

export interface VerifierSpec {
  verifier_id: string
  stage: VerifierStage
  check_window: CheckWindow
  display_label: string
  display_tone: 'info' | 'success' | 'warning' | 'danger' | 'neutral'
}

export interface VerifierOutcome {
  state: VerifierState
  matched_at_ms: number | null
  match_event_id: string | null
}

export interface CommandInstance {
  instance_id: string
  correlation_key: Array<string | number>
  t0_ms: number
  cmd_event_id: string
  verifier_set: { verifiers: VerifierSpec[] }
  outcomes: Record<string, VerifierOutcome>
  stage: InstanceStage
}

// ---- Doppler tracking ----

export type DopplerMode = 'disconnected' | 'connected' | 'sim'

export interface DopplerCorrection {
  ts_ms: number
  station_id: string
  satellite: string
  mode: DopplerMode
  range_rate_mps: number
  rx_hz: number
  rx_shift_hz: number
  rx_tune_hz: number
  tx_hz: number
  tx_shift_hz: number
  tx_tune_hz: number
}

export interface TrackingStatusMessage {
  type: 'status'
  mode: DopplerMode
  last_error: string
  last_tick_ms: number
}

export interface TrackingDopplerMessage {
  type: 'doppler'
  doppler: DopplerCorrection
  ts_ms: number
}

export interface TrackingErrorMessage {
  type: 'error'
  error: string
}

export type TrackingWsMessage =
  | TrackingStatusMessage
  | TrackingDopplerMessage
  | TrackingErrorMessage
