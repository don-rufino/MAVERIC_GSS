/**
 * Downlink Preview — 3-pane focus workbench, live-wired.
 *
 *   ┌──────────────────────────────────────────────────────────────────┐
 *   │ topbar · DOWNLINK · search · tools⋯                               │
 *   ├──────┬─────────────────────────────────┬───────────────────────┤
 *   │ pick │ FOCUS                            │ COMMAND DECK          │
 *   │ 200  │  header · kind · src · name      │ 400 wide              │
 *   │      │  [FULL] [THUMB]   ← leaf pick    │                       │
 *   │ live │  progressive preview             │  state-aware primary  │
 *   │ idle │  chunk timeline                  │  range stage          │
 *   │ done │  missing ranges                  │  cnt / del            │
 *   │      │                                  │  standalone ops       │
 *   │      │                                  │  chunk size lock      │
 *   │      │                                  │  staged queue         │
 *   ├──────┴─────────────────────────────────┴───────────────────────┤
 *   │ events (collapsible) — file-only RX packet log                    │
 *   └──────────────────────────────────────────────────────────────────┘
 *
 * Live data sources (all reused from existing plugin/shared code):
 *   - useImageFiles + useFlatFiles('aii'/'mag')  — file lists
 *   - usePluginServices()                        — packets, queue, send
 *   - shared/fileKinds.ts (fileCaps)             — per-kind cmd_id registry
 *   - shared/dialogs/ConfirmDialog               — destructive confirm
 *   - shared/overlays/StatusToast                — feedback
 *   - files/JsonPreview, files/MagPreview        — file content panes
 *
 * Operator flow assumptions:
 *   - One transfer at a time (no parallel) — staged queue shows next-up.
 *   - Count first (cnt) before get (operator workflow). Primary action is
 *     state-aware: Count → Get all → Pull missing → Up to date.
 *   - Capture happens out-of-pass — lives in the tools menu, not as a
 *     primary CTA.
 *   - TX overrides (kind / filename / dest folder) persist across picker
 *     clicks. Picker drives what's *shown*; deck drives what's *targeted*.
 *
 * Out-of-scope (global GSS shell concerns):
 *   beacon ages, alarms, link state, session id, build version.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Camera, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Download, Eraser, FileBox,
  FileJson, Image as ImageIcon, Lock, Monitor, MoreHorizontal, Power, Radio,
  RefreshCcw, Search, Send, Trash2, X,
} from 'lucide-react';
import { colors } from '@/lib/colors';
import { usePluginServices } from '@/hooks/usePluginServices';
import { ConfirmDialog } from '@/components/shared/dialogs/ConfirmDialog';
import { showToast } from '@/components/shared/overlays/StatusToast';
import { useImageFiles, useFlatFiles, useFileChunks } from '../files/FileChunkContext';
import { JsonPreview } from '../files/JsonPreview';
import { MagPreview } from '../files/MagPreview';
import { filesEndpoint } from '../files/helpers';
import { fileCaps } from '../shared/fileKinds';
import { withExtension } from '../shared/extensions';
import { FilenameInput } from '../shared/FilenameInput';
import { useFileChunkSet, type ChunkSetTarget } from '../shared/useFileChunkSet';
import { computeMissingRanges } from '../shared/missingRanges';
import { packetPayloadText } from '@/lib/rxPacket';
import type { TxArgSchema } from '@/lib/types';
import type { FileLeaf, ImagePair } from '../files/types';

// ─── MOCK DATA ───────────────────────────────────────────────────────

type Kind = 'image' | 'aii' | 'mag';
type Source = 'HLNV' | 'ASTR';
type FileState = 'discovered' | 'counted' | 'in-flight' | 'complete';
type Leaf = 'full' | 'thumb';

interface LeafData { received: number; total: number; chunkSize: number }
interface ImageFile { id: string; kind: 'image'; source: Source; stem: string; full: LeafData; thumb: LeafData | null; ageS: number; }
interface FlatFile  { id: string; kind: 'aii' | 'mag'; source: Source; filename: string; received: number; total: number; chunkSize: number; ageS: number; }
type DFile = ImageFile | FlatFile;

// FILES list is now driven by `useImageFiles()` + `useFlatFiles('aii')`
// + `useFlatFiles('mag')` inside the page component (see `liveFiles`).
// Mock array removed in the live wire-up.

// `AII_RAW` mock removed — `JsonPreview` now fetches from
// /api/plugins/files/preview?kind=aii via the shared component.

type VerifierStage = 'released' | 'accepted' | 'received' | 'complete';
interface StagedRow {
  num: number;
  cmdId: string;
  sub: string;
  stage: VerifierStage;
  pct: number;
  /** Index in the unfiltered pendingQueue — needed for removeQueueItem. */
  index: number;
}

// `STAGED` is now derived from `pendingQueue` inside the page
// component (see `liveStaged`). Live verifier stages are simplified —
// `released` for queued, `accepted` for currently sending. Full verifier
// lifecycle (REL → ACC → RCV → OK) lives in cmd_verifier RX events,
// which need a separate join step (deferred).

type PType = 'CMD' | 'CHUNK' | 'RES' | 'ACK' | 'TLM' | 'REQ';
interface ActivityRow {
  tsRel: number;     // seconds-from-start (display only)
  dir: 'TX' | 'RX';
  ptype: PType;
  src: string;       // GS / HLNV / ASTR / UPPM / LPPM / EPS
  cmd: string;
  meta: string;
}
// Predicate: command originates from the Files page (imaging / AII /
// MAG transfer ops plus the tools-menu cam/lcd/mag controls). Drives
// both the Staged queue filter and the Activity log filter so every
// command staged from this page is visible to the operator.
// Excludes TLM beacons, EPS HK, MTQ/GNC/PPM ops which belong to other
// pages.
const FILES_PAGE_CMD_RE = /^(img|aii|mag|cam|lcd)_/;
function isFilesPageCmd(cmd: string): boolean {
  return FILES_PAGE_CMD_RE.test(cmd);
}

// `ACTIVITY` is now derived from live `packets` inside the page
// component (see `liveActivity`). The module-level mock array was removed
// in the live wire-up.

const PTYPE_TONE: Record<PType, string> = {
  CHUNK: colors.success,  // RX data
  RES:   colors.success,  // RES per badgeToneMap
  ACK:   colors.info,     // ACK per badgeToneMap
  TLM:   colors.active,   // TLM per badgeToneMap
  CMD:   colors.neutral,  // CMD per badgeToneMap
  REQ:   colors.neutral,  // REQ per badgeToneMap
};

// Kind tones — anchored to the existing GSS convention:
//   IMG → active   (cyan)  ← matches FilenameInput full-leaf tag
//   AII → success  (green) ← matches FilesTable KindBadge for AII
//   MAG → neutral  (gray)  ← matches FilesTable KindBadge for MAG
// Yellow (warning) is reserved for caution/guarded states (lock, novel
// filename, lcd_clear, thumb leaf), not kind identification.
const KIND_TONE: Record<Kind, string> = { image: colors.active, aii: colors.success, mag: colors.neutral };
const KIND_LABEL: Record<Kind, string> = { image: 'IMG', aii: 'AII', mag: 'MAG' };
const KIND_EXT:   Record<Kind, string> = { image: '.jpg', aii: '.json', mag: '.npz' };
// UI-state persistence: the deck's `txKindOverride` and per-file
// `cntChunkSizes` lock cache used to live in browser localStorage
// (per-browser, lost on cache clear). Now they're persisted server-
// side under `<log_dir>/files/.ui_state.json` via the
// `/api/plugins/files/ui-state` GET/PUT endpoint, so the state lives
// with the GS data and is shared across browsers.
const UI_STATE_ENDPOINT = '/api/plugins/files/ui-state';

interface PersistedUiState {
  txKindOverride?: Kind | null;
  cntChunkSizes?: Record<string, string>;
  activeLeaf?: Leaf;
}

function isKind(v: unknown): v is Kind {
  return v === 'image' || v === 'aii' || v === 'mag';
}

function isLeaf(v: unknown): v is Leaf {
  return v === 'full' || v === 'thumb';
}

async function loadUiState(): Promise<PersistedUiState> {
  try {
    const r = await fetch(UI_STATE_ENDPOINT);
    if (!r.ok) return {};
    const raw = (await r.json()) as unknown;
    if (!raw || typeof raw !== 'object') return {};
    const obj = raw as Record<string, unknown>;
    const out: PersistedUiState = {};
    if (isKind(obj.txKindOverride) || obj.txKindOverride === null) {
      out.txKindOverride = obj.txKindOverride as Kind | null;
    }
    const cs = obj.cntChunkSizes;
    if (cs && typeof cs === 'object' && !Array.isArray(cs)) {
      const map: Record<string, string> = {};
      for (const [k, v] of Object.entries(cs as Record<string, unknown>)) {
        if (typeof v === 'string') map[k] = v;
      }
      out.cntChunkSizes = map;
    }
    if (isLeaf(obj.activeLeaf)) {
      out.activeLeaf = obj.activeLeaf;
    }
    return out;
  } catch {
    return {};
  }
}

async function saveUiState(state: PersistedUiState): Promise<void> {
  try {
    await fetch(UI_STATE_ENDPOINT, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state),
    });
  } catch { /* offline tolerant — next write will pick up the latest in-memory state */ }
}

// Key for the per-file cnt-chunk_size cache — stage-time snapshot of
// the operator's typed `chunk_size` arg, used to engage the lock as
// soon as the cnt has been staged (before any chunks have arrived).
function cntKey(kind: Kind, source: string | null | undefined, filename: string): string {
  return `${kind}|${source ?? ''}|${filename}`;
}
const KINDS: ReadonlyArray<Kind> = ['image', 'aii', 'mag'];

const STATE_TONE: Record<FileState, string> = {
  discovered: colors.neutral,
  counted:    colors.info,
  'in-flight': colors.info,
  complete:   colors.success,
};

// Verifier-stage tones — anchored to the badgeToneMap semantic system:
//   released → neutral (just queued, no feedback yet)
//   accepted → info    (uplink ACK observed)
//   received → info    (REQ from FSW; advisory same family)
//   complete → success (RES — confirmed nominal)
const STAGE_COLOR: Record<VerifierStage, string> = {
  released: colors.neutral,
  accepted: colors.info,
  received: colors.info,
  complete: colors.success,
};
const STAGE_LABEL: Record<VerifierStage, string> = {
  released: 'REL', accepted: 'ACC', received: 'RCV', complete: 'OK',
};

function pct(received: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((received / total) * 100);
}
function aggregateTotals(f: DFile): { received: number; total: number; pct: number } {
  if (f.kind === 'image') {
    const r = (f.full?.received ?? 0) + (f.thumb?.received ?? 0);
    const t = (f.full?.total ?? 0) + (f.thumb?.total ?? 0);
    return { received: r, total: t, pct: pct(r, t) };
  }
  return { received: f.received, total: f.total, pct: pct(f.received, f.total) };
}
function leafTotals(f: DFile, leaf: Leaf): LeafData {
  if (f.kind === 'image') {
    // Same fallback as the ProgressivePreview / lockedChunkSize paths:
    // a placeholder thumb (received=0, total=0) shouldn't drive the
    // local leaf data — fall back to full so progress, missing-range
    // bar, and the per-leaf state badge all read coherently.
    if (leaf === 'thumb' && f.thumb && (f.thumb.received > 0 || f.thumb.total > 0)) {
      return f.thumb;
    }
    return f.full;
  }
  return { received: f.received, total: f.total, chunkSize: f.chunkSize };
}
function leafState(leaf: LeafData): FileState {
  if (leaf.total === 0) return 'discovered';
  if (leaf.received === 0) return 'counted';
  if (leaf.received < leaf.total) return 'in-flight';
  return 'complete';
}
function fileName(f: DFile): string { return f.kind === 'image' ? f.stem : f.filename; }
function leafFilename(f: DFile, leaf: Leaf): string {
  if (f.kind === 'image' && leaf === 'thumb') return `tn_${f.stem}`;
  return fileName(f);
}
// Distinguish a real (counted or partially-received) thumb from the
// backend's placeholder leaf. The status adapter always returns a
// thumb entry so the JSON shape is uniform; using `f.thumb != null`
// alone treats every image as "paired", which paints `+tn` on entries
// that have never had a thumbnail. Real thumbs carry total > 0 (after
// the cnt RES populates `thumb_num_chunks`) or received > 0 (chunks
// arrived ahead of the count).
function hasRealThumb(f: ImageFile): boolean {
  return f.thumb !== null && (f.thumb.total > 0 || f.thumb.received > 0);
}
function fileOverallState(f: DFile): FileState {
  const t = aggregateTotals(f);
  if (t.total === 0) return 'discovered';
  if (t.received === 0) return 'counted';
  if (t.received < t.total) return 'in-flight';
  return 'complete';
}
// ─── LIVE-DATA ADAPTERS ──────────────────────────────────────────────
// Translate the platform/file-context shapes into the preview's `DFile`
// model. Live `total` and `chunk_size` are nullable (null = "discovered,
// not counted"); the preview's state machine treats `total === 0` as
// discovered, so null → 0 keeps the existing behaviour. Source defaults
// to 'HLNV' when null (most common in real ops).

function asSource(s: string | null | undefined): Source {
  return s === 'ASTR' ? 'ASTR' : 'HLNV';
}

function liveLeaf(l: FileLeaf): LeafData {
  return {
    received: l.received,
    total: l.total ?? 0,
    chunkSize: l.chunk_size ?? 150,
  };
}

function ageFrom(lastMs: number | null | undefined, nowMs: number): number {
  if (lastMs == null) return 9999;
  return Math.max(0, Math.floor((nowMs - lastMs) / 1000));
}

function adaptImagePair(p: ImagePair, nowMs: number): ImageFile {
  return {
    id: p.id,
    kind: 'image',
    source: asSource(p.source),
    stem: p.stem,
    full: liveLeaf(p.full),
    thumb: p.thumb ? liveLeaf(p.thumb) : null,
    ageS: ageFrom(p.last_activity_ms, nowMs),
  };
}

function adaptFlatFile(f: FileLeaf, nowMs: number): FlatFile | null {
  if (f.kind === 'image') return null; // image files come via ImagePair
  return {
    id: f.id,
    kind: f.kind,
    source: asSource(f.source),
    filename: f.filename,
    received: f.received,
    total: f.total ?? 0,
    chunkSize: f.chunk_size ?? 150,
    ageS: ageFrom(f.last_activity_ms, nowMs),
  };
}

// ─── PAGE ────────────────────────────────────────────────────────────

type FilterKind = 'all' | Kind;
const FILTERS: ReadonlyArray<{ id: FilterKind; label: string }> = [
  { id: 'all',   label: 'ALL' },
  { id: 'image', label: 'IMG' },
  { id: 'aii',   label: 'AII' },
  { id: 'mag',   label: 'MAG' },
];

// On-board folder destination for image commands (cnt/get/del). Mirrors
// the IMG `destination` arg from mission.yml:
//   0 = prestored_images (factory-loaded on-board)
//   1 = captured_images  (from cam_capture)
//   2 = thumbnails       (downscaled siblings of captured images)
// Active leaf (FULL/THUMB) seeds the default; operator overrides anytime.
type Dest = 0 | 1 | 2;
const DEST_OPTIONS: ReadonlyArray<{ id: Dest; label: string; title: string }> = [
  { id: 0, label: 'STR',  title: '0 · prestored_images (factory-loaded)' },
  { id: 1, label: 'FULL', title: '1 · captured_images (full)' },
  { id: 2, label: 'THMB', title: '2 · thumbnails' },
];

export default function DownlinkPreview() {
  // Live data hooks. usePluginServices is mounted at App level; the
  // FileChunkProvider wraps all maveric plugin pages (see providers.ts)
  // so useImageFiles / useFlatFiles work here without extra setup.
  // Destructured to match FilesPage / ImagingPage convention — children
  // receive primitives, not a `services` blob.
  const {
    packets,
    pendingQueue,
    sendProgress,
    queueCommand,
    sendAll,
    abortSend,
    removeQueueItem,
    fetchSchema,
    txConnected,
  } = usePluginServices();
  const imageFiles = useImageFiles();
  const aiiFiles   = useFlatFiles('aii');
  const magFiles   = useFlatFiles('mag');
  const { setLastTouchedFlatKind } = useFileChunks();

  // Compose into the preview's DFile shape. Recomputed on every render
  // — packets/files arrays are reference-stable from the providers, and
  // the adapter is cheap.
  const liveFiles: DFile[] = useMemo(() => {
    const now = Date.now();
    const out: DFile[] = [];
    for (const p of imageFiles.files) out.push(adaptImagePair(p, now));
    for (const f of aiiFiles.files) {
      const a = adaptFlatFile(f, now);
      if (a) out.push(a);
    }
    for (const f of magFiles.files) {
      const a = adaptFlatFile(f, now);
      if (a) out.push(a);
    }
    return out;
  }, [imageFiles.files, aiiFiles.files, magFiles.files]);

  // Live staged queue = pendingQueue filtered to file commands. The
  // unfiltered index is preserved on each row so `removeQueueItem(index)`
  // / `abortSend` target the right backing item.
  const liveStaged: StagedRow[] = useMemo(() => {
    const sp = sendProgress;
    const out: StagedRow[] = [];
    pendingQueue.forEach((item, index) => {
      if (item.type !== 'mission_cmd') return;
      if (!isFilesPageCmd(item.cmd_id)) return;
      const inFlight = sp != null && sp.current === item.cmd_id && out.length === 0;
      const sub = (item.parameters ?? [])
        .filter(p => !p.display_only)
        .map(p => `${p.name}=${p.value}`)
        .join(' · ');
      out.push({
        num: item.num,
        cmdId: item.cmd_id,
        sub,
        stage: inFlight ? 'accepted' : 'released',
        pct: inFlight && sp && sp.total > 0 ? Math.round((sp.sent / sp.total) * 100) : 0,
        index,
      });
    });
    return out;
  }, [pendingQueue, sendProgress]);

  // Live activity = downlink RX packets filtered to file commands. TX
  // events live in the Staged section (pendingQueue), not here — same
  // split as existing RxLogPanel + QueuePanel. Capped at 50 most recent.
  const liveActivity: ActivityRow[] = useMemo(() => {
    const out: ActivityRow[] = [];
    const startMs = packets[0]?.received_at_ms ?? 0;
    for (const p of packets) {
      if (p.is_echo) continue;
      const facts = (p.mission?.facts ?? {}) as Record<string, unknown>;
      const header = (facts.header ?? {}) as Record<string, unknown>;
      const cmdId = String(header.cmd_id ?? '');
      if (!cmdId || !isFilesPageCmd(cmdId)) continue;
      const rawPty = String(header.ptype ?? 'CMD');
      const ptype: PType = (rawPty in PTYPE_TONE) ? (rawPty as PType) : 'CMD';
      // Args from packet facts/parameters formatted as `k=v  k=v` —
      // mirrors the legacy RxLogPanel display (key/value pairs from
      // mission.facts.header for RES/ACK, or ParamUpdate names/values
      // for chunk responses). Keeps pkt # as a fallback when nothing
      // structured is available.
      const args = packetPayloadText(p, { compact: true });
      out.push({
        tsRel: p.received_at_ms != null ? Math.max(0, (p.received_at_ms - startMs) / 1000) : 0,
        dir: 'RX',
        ptype,
        src: String(header.src ?? '?'),
        cmd: cmdId,
        meta: args || `pkt #${p.num}`,
      });
    }
    return out.slice(-50);
  }, [packets]);

  // Seed focus from existing per-kind selectedId when the operator opens
  // the Preview tab — inherits whatever's already selected in Imaging /
  // Files pages so they don't have to re-pick.
  const [focusedId, setFocusedId] = useState<string>(() => (
    imageFiles.selectedId || aiiFiles.selectedId || magFiles.selectedId || ''
  ));

  // If a selection appears on another page after this page mounts (e.g.,
  // operator selected on Imaging then opened Preview), auto-focus it
  // when nothing is focused here yet. Doesn't override an explicit pick.
  useEffect(() => {
    if (focusedId) return;
    const candidate = imageFiles.selectedId || aiiFiles.selectedId || magFiles.selectedId;
    if (candidate) setFocusedId(candidate);
  }, [focusedId, imageFiles.selectedId, aiiFiles.selectedId, magFiles.selectedId]);
  const [activeLeaf, setActiveLeaf] = useState<Leaf>('full');
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<FilterKind>('all');
  const [captureOpen, setCaptureOpen] = useState(false);
  const [lcdOpen, setLcdOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [activityExpanded, setActivityExpanded] = useState(false);
  const [pickerCollapsed, setPickerCollapsed] = useState(false);
  // Per-file cnt chunk_size memory. Written at stage time with the
  // operator's typed chunk_size, but the lock only ENGAGES once the
  // corresponding cnt RES has been processed — i.e. once liveFiles
  // shows the leaf with `total > 0`. Backend doesn't echo chunk_size
  // in the cnt RES (only num_chunks via `set_total`) and doesn't fill
  // `chunk_sizes[ref]` until `feed_chunk`, so the frontend cache is
  // the only place the operator's intended slicing lives until chunks
  // start flowing. Keyed by `(kind, source, filename)`. For image
  // pairs, both leaves get the same entry so toggling FULL ↔ THUMB
  // after a cnt keeps the lock engaged.
  const [cntChunkSizes, setCntChunkSizes] = useState<Record<string, string>>({});
  // "Have we ever seen this key in liveFiles?" Used by the GC effect
  // to distinguish in-flight cnts (entry written but RES hasn't
  // landed yet, so the file isn't in liveFiles) from forgotten files
  // (entry was confirmed once and the file is now gone). Without this,
  // a freshly staged cnt gets pruned within 250ms — well before the
  // RF round-trip — and the lock falls through to the chunk-arrival
  // path, defeating the cnt-RES-driven lock.
  const everSeenCntKeysRef = useRef<Set<string>>(new Set());
  // Two-scope delete: 'local' forgets accumulated GS-side chunks via
  // HTTP DELETE; 'spacecraft' uplinks the mission's *_delete command.
  // Picker row trash → local (matches legacy FilesPage semantics).
  // Deck "del" button → spacecraft (operator is composing TX commands).
  // `targetFilename` overrides `fileName(file)` for spacecraft deletes
  // — required for image thumbs because `f.stem` always carries the
  // FULL leaf's filename, but the operator on the THUMB leaf wants
  // their `*_delete` command targeted at `tn_<stem>`.
  const [pendingDelete, setPendingDelete] = useState<{
    file: DFile;
    scope: 'local' | 'spacecraft';
    targetFilename?: string;
  } | null>(null);
  // Schema-driven inputs for StandaloneOps. Mirrors what FilesTxControls
  // does on the legacy page — `/api/schema` is the single source of
  // truth for arg shape per cmd_id, so a schema change propagates
  // without code edits here.
  const [schema, setSchema] = useState<Record<string, Record<string, unknown>> | null>(null);
  useEffect(() => { fetchSchema().then(setSchema).catch(() => {}); }, [fetchSchema]);

  const [uiHydrated, setUiHydrated] = useState(false);

  // TX targeting state:
  //   - txKindOverride:     IMG/AII/MAG override; null = follow focus.
  //                         Persisted server-side via /api/plugins/files/
  //                         ui-state so the kind pick survives page
  //                         navigation, reloads, and browser changes.
  //   - txFilenameOverride: typed filename; null = derive from focus+leaf
  //   - txDestOverride:     STR/FULL/THMB folder (IMG only); null = leaf
  //   (node selection lives in `imageFiles.destNode` from the provider)
  const [txKindOverride, setTxKindOverride] = useState<Kind | null>(null);
  const [txFilenameOverride, setTxFilenameOverride] = useState<string | null>(null);
  const [txDestOverride, setTxDestOverride] = useState<Dest | null>(null);
  // Node selection lives in the provider (`imageFiles.destNode`).
  // It already persists across kind switches AND across pages, so this
  // page reads from it directly via `txNode` below. Earlier this was
  // mirrored into a page-local `pageNode` with a bidirectional sync
  // effect; the mirror was redundant — the only state that needed
  // outliving the page mount was already provider-owned.

  // Hydrate persisted UI state from the server on mount. Failures are
  // swallowed by `loadUiState` — the page continues with empty defaults.
  // `uiHydrated` gates the save-on-change effect so we don't immediately
  // overwrite the server file with the empty pre-load state.
  useEffect(() => {
    let alive = true;
    void loadUiState().then(state => {
      if (!alive) return;
      if (state.txKindOverride !== undefined) setTxKindOverride(state.txKindOverride);
      if (state.cntChunkSizes) setCntChunkSizes(state.cntChunkSizes);
      // Hydrate the operator's last leaf preference. If the file
      // they're focused on doesn't have a real thumb, the
      // `hasRealThumb` fallback paths in `leafTotals`,
      // `focusedImageLeaf`, and `lockedChunkSize` already coerce to
      // full — so persisting 'thumb' is safe even when the current
      // focus is a thumb-less image.
      if (state.activeLeaf) setActiveLeaf(state.activeLeaf);
      setUiHydrated(true);
    });
    return () => { alive = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Save-on-change with a small debounce so rapid kind chip clicks
  // and bulk cnt staging don't hammer the endpoint. Last write wins.
  // GC: prune cnt-tracked entries that were once confirmed (their key
  // appeared in liveFiles at some point this session) but no longer
  // resolve to a live file — typically a server-side delete or a
  // cleared store. In-flight cnts (entry written but file not yet in
  // liveFiles, because the RES hasn't landed) are protected: their
  // keys won't be in `everSeenCntKeysRef` yet, so the prune leaves
  // them alone until the RES lands and confirms them, OR the operator
  // clears them via local-delete. Skipping prune on empty liveFiles
  // also guards against the transient gap during a refetch.
  useEffect(() => {
    if (!uiHydrated) return;
    const t = setTimeout(() => {
      if (liveFiles.length === 0) {
        // Provider mid-refetch — don't prune against an empty list.
        void saveUiState({ txKindOverride, cntChunkSizes, activeLeaf });
        return;
      }
      const liveKeys = new Set<string>();
      for (const f of liveFiles) {
        if (f.kind === 'image') {
          liveKeys.add(cntKey('image', f.source, f.stem));
          if (hasRealThumb(f)) {
            liveKeys.add(cntKey('image', f.source, `tn_${f.stem}`));
          }
        } else {
          liveKeys.add(cntKey(f.kind, f.source, f.filename));
        }
      }
      // Promote currently-live keys to "seen" — once a cnt RES has
      // confirmed a file, future disappearances are real cleanup, not
      // pre-RES transients.
      const seen = everSeenCntKeysRef.current;
      for (const k of liveKeys) seen.add(k);
      const pruned: Record<string, string> = {};
      let anyDropped = false;
      for (const [k, v] of Object.entries(cntChunkSizes)) {
        if (liveKeys.has(k) || !seen.has(k)) {
          // Either currently live, or never seen yet (in-flight cnt).
          pruned[k] = v;
        } else {
          anyDropped = true;
        }
      }
      // Sync the in-memory map too so `lockedChunkSize` doesn't latch
      // on stale keys that were just pruned from disk.
      if (anyDropped) setCntChunkSizes(pruned);
      void saveUiState({ txKindOverride, cntChunkSizes: pruned, activeLeaf });
    }, 250);
    return () => clearTimeout(t);
  }, [uiHydrated, txKindOverride, cntChunkSizes, activeLeaf, liveFiles]);
  // Chunk size — passed as `chunk_size` arg on cnt/get commands.
  // Default 150B per AX100 Mode 5 / CSP v1 framing. Operator can edit
  // freely until the first chunk lands; once `received > 0` for the
  // resolved file's active leaf, the input locks to that file's
  // confirmed size (mixing would corrupt the local re-assembly and
  // the spacecraft's slicing). Lock releases when the filename no
  // longer matches a known file (typed-elsewhere or local-deleted).
  const [chunkSize, setChunkSize] = useState<string>('150');

  // Click-to-restage: chunk-timeline missing-range chip → range form.
  // Stores a (start, count, key) — `key` re-mounts the inputs so a
  // freshly-derived defaultValue actually shows up.
  const [pendingRestage, setPendingRestage] = useState<{ start: number; count: number; key: number } | null>(null);

  const focused = liveFiles.find(f => f.id === focusedId) ?? null;

  // Look up the original FileLeaf for the focused AII/MAG file so we can
  // pass it to the existing JsonPreview / MagPreview components instead
  // of duplicating their fetch+render logic.
  const focusedLeaf: FileLeaf | null = useMemo(() => {
    if (!focused || focused.kind === 'image') return null;
    const list = focused.kind === 'aii' ? aiiFiles.files : magFiles.files;
    return list.find(f => f.id === focused.id) ?? null;
  }, [focused, aiiFiles.files, magFiles.files]);

  // Image focus needs the original FileLeaf (full or thumb) so the
  // preview <img> can hit `/api/plugins/files/preview?kind=image&...`
  // and the ChunkTimeline can fetch real received-chunk indices. Falls
  // back to the full leaf when the operator's active leaf is thumb but
  // no real thumb exists yet (the backend status adapter returns a
  // placeholder thumb for every pair, which would otherwise route the
  // <img> at a non-existent file).
  const focusedImageLeaf: FileLeaf | null = useMemo(() => {
    if (!focused || focused.kind !== 'image') return null;
    const pair = imageFiles.files.find(p => p.id === focused.id);
    if (!pair) return null;
    const wantThumb = activeLeaf === 'thumb' && pair.thumb && (
      pair.thumb.received > 0 || (pair.thumb.total ?? 0) > 0
    );
    return wantThumb ? pair.thumb : pair.full;
  }, [focused, activeLeaf, imageFiles.files]);

  // Memoized so high-cadence RX (chunk fan-out, send-progress ticks)
  // doesn't re-sort the full file list on every page render — `liveFiles`
  // is reference-stable from its own `useMemo`, so this only recomputes
  // when files change or the operator edits the filter/search.
  const pickerFiles = useMemo(() => {
    const q = search.trim().toLowerCase();
    const byFilter = filter === 'all' ? [...liveFiles] : liveFiles.filter(f => f.kind === filter);
    const filtered = q ? byFilter.filter(f => fileName(f).toLowerCase().includes(q)) : byFilter;
    return filtered.sort((a, b) => {
      const sa = fileOverallState(a), sb = fileOverallState(b);
      const order: Record<FileState, number> = { 'in-flight': 0, counted: 1, discovered: 2, complete: 3 };
      if (order[sa] !== order[sb]) return order[sa] - order[sb];
      return a.ageS - b.ageS;
    });
  }, [liveFiles, filter, search]);

  // When focusing a non-image (or an image without thumb), force the leaf
  // back to full so the toggle never lands on a non-existent thumb.
  // Picking a file also drives the deck: kind override snaps to the
  // file's kind, and the typed-filename override clears so the deck
  // re-derives from focus + leaf (i.e. the picker click is treated as
  // an explicit "I'm working on this file now").
  function selectFile(id: string) {
    const f = liveFiles.find(x => x.id === id);
    if (!f || f.kind !== 'image' || !hasRealThumb(f)) setActiveLeaf('full');
    if (f) setTxKindOverride(f.kind);
    setTxFilenameOverride(null);
    setFocusedId(id);
    // Cross-page selection sync — same provider-managed selectedId/
    // lastTouchedFlatKind state used by ImagingPage / FilesPage. Picking
    // a file here surfaces in those pages on the operator's next visit.
    if (f) {
      if (f.kind === 'image') {
        imageFiles.setSelectedId(id);
        // Auto-sync the cross-page provider to the focused file's
        // source so the deck routes correctly without a manual node
        // click. Same UX as the legacy ImagingPage.
        if (f.source) imageFiles.setDestNode(f.source);
      } else if (f.kind === 'aii') {
        aiiFiles.setSelectedId(id);
        setLastTouchedFlatKind('aii');
      } else {
        magFiles.setSelectedId(id);
        setLastTouchedFlatKind('mag');
      }
    }
    // Per "keep last used" — TX kind/filename/dest overrides persist
    // across picker clicks. Operator manages them via the deck. Picker
    // selection changes what's *shown*, not what the deck *targets*.
    setPendingRestage(null);
  }

  function pickRangeFromChunk(start: number, count: number) {
    setPendingRestage({ start, count, key: Date.now() });
  }

  // Single staging entrypoint — mirrors the existing `TxControlsPanel`
  // pattern: build a {cmd_id, args, packet:{dest}} payload and hand to
  // queueCommand. Backend expects Record<string, string>, so callers
  // must stringify upstream. Toast feedback matches FilesPage / ImagingPage.
  function stageCmd(cmd_id: string, args: Record<string, string> = {}, dest?: string) {
    const target = dest ?? 'HLNV'; // default node — operator can override per kind
    // Snapshot the operator's typed chunk_size on every cnt — this is
    // the value the backend's slicing was committed to (img_cnt_chunks
    // / aii_cnt_chunks / mag_cnt_chunks all carry chunk_size). Stored
    // here at stage time so the value is available later, but the lock
    // only engages once the cnt RES has been processed (see
    // `lockedChunkSize` — gates on `leaf.total > 0`). Stage-time write
    // is just a parking spot; the lock visibly flips when the
    // spacecraft acknowledges the count, not when the cmd is queued.
    const cntMatch = cmd_id.match(/^(img|aii|mag)_cnt_chunks$/);
    if (cntMatch && args.filename && args.chunk_size) {
      const kind: Kind = cntMatch[1] === 'img' ? 'image' : (cntMatch[1] as Kind);
      // Normalise to the same identity the read site uses: image keys
      // by `stem` (`tn_<stem>` for thumb), aii/mag by full filename.
      // Without this, image cnt-tracked locks never engage because the
      // command's `args.filename` carries the `.jpg` extension but
      // `lockedChunkSize` looks up by `resolvedFile.stem`.
      const ext = fileCaps(kind).extension;
      const lookupName = (kind === 'image' && ext && args.filename.endsWith(ext))
        ? args.filename.slice(0, -ext.length)
        : args.filename;
      // Key the lock by the *file's* source, not the operator's TX
      // dest selector. The read site (`lockedChunkSize`) uses
      // `resolvedFile.source`, so write/read must agree on which
      // source they record. When `txNode` and the focused file's
      // source diverge (operator changed dest after focusing a file),
      // keying by `target` would silently drop the lock. Falls back
      // to `target` when no matching file is in `liveFiles` yet —
      // novel filename case where operator has just typed a name.
      const fileSource = liveFiles.find(
        f => f.kind === kind && fileName(f) === lookupName,
      )?.source ?? target;
      setCntChunkSizes(prev => {
        const next = { ...prev, [cntKey(kind, fileSource, lookupName)]: args.chunk_size };
        // Image pair: cnt for one leaf returns counts for both, so the
        // operator's intended chunk_size applies to both. Mirror the
        // entry to the sibling so toggling leaves keeps the lock.
        if (kind === 'image') {
          const sibling = lookupName.startsWith('tn_') ? lookupName.slice(3) : `tn_${lookupName}`;
          next[cntKey('image', fileSource, sibling)] = args.chunk_size;
        }
        return next;
      });
    }
    queueCommand({ cmd_id, args, packet: { dest: target } });
    showToast(`Staged ${cmd_id}`, 'info');
  }

  // Auto-switch the leaf view based on the typed filename:
  //   `tn_*` → THUMB
  //   else   → FULL
  // Mirrors `FilenameInput.tsx` which auto-derives the dest arg from the
  // thumb prefix. Operator never has to flip the leaf toggle manually
  // when typing — it tracks the input.
  function changeFilename(stem: string) {
    setTxFilenameOverride(stem);
    setPendingRestage(null);
    if (stem.startsWith('tn_')) setActiveLeaf('thumb');
    else                        setActiveLeaf('full');
  }

  // Resolved TX kind: explicit override beats focus.
  const txKind: Kind = txKindOverride ?? focused?.kind ?? 'image';

  // Default filename derived from focus + leaf, ext-stripped (operator
  // edits the stem; the ext is implicit per kind). Drives auto-fill on
  // every picker click and leaf toggle. The gate accepts both:
  //   • txKindOverride === null (no override at all)
  //   • txKindOverride === focused.kind (operator picked this file —
  //     selectFile sets the override to the file's kind, which would
  //     otherwise fall through to caps.defaultFilename and lose the
  //     focus-derived name)
  // Only when the operator explicitly switches deck kind to something
  // that doesn't match focus do we fall back to the kind's canonical
  // default (e.g. AII → `transmit_dir`).
  const defaultFilenameStem =
    focused && (txKindOverride === null || txKindOverride === focused.kind)
      ? leafFilename(focused, activeLeaf).replace(/\.[^.]+$/, '')
      : (fileCaps(txKind).defaultFilename ?? '');
  const txFilenameStem = txFilenameOverride ?? defaultFilenameStem;
  const txFilename = txFilenameStem ? `${txFilenameStem}${KIND_EXT[txKind]}` : '';

  // Two-pass resolution:
  //   1. `baseResolvedFile` matches by (kind, filename) only — used to
  //      seed `txNode` when the operator hasn't explicitly routed.
  //   2. `resolvedFile` narrows to the source-matching entry once
  //      `txNode` is known. For images, also handles the `tn_` prefix
  //      fallback (typed `tn_out` → match `out`'s pair).
  // The two-pass shape exists to break a TDZ: txNode wants
  // `resolvedFile.source` as a fallback; resolvedFile wants `txNode`
  // for source disambiguation. Splitting lets each step see only the
  // values declared above it.
  const baseResolvedFile: DFile | null = (() => {
    if (!txFilename) return null;
    const byNameKind = (f: DFile) => f.kind === txKind && fileName(f) === txFilename;
    const direct = liveFiles.find(byNameKind);
    if (direct) return direct;
    // Thumb-prefix fallback: typed `tn_out.jpg` should match the pair
    // whose backend stem is `out.jpg` (the backend strips the
    // configured prefix when bucketing pairs but keeps the extension —
    // see mission/files/adapters.py::_imaging stem derivation). So
    // strip `tn_` from the FULL filename (with extension) and compare
    // against `f.stem` directly.
    if (txKind === 'image' && txFilename.startsWith('tn_')) {
      const expectedStem = txFilename.slice(3);
      return liveFiles.find(f => f.kind === 'image' && f.stem === expectedStem) ?? null;
    }
    return null;
  })();
  const resolvedLeaf: Leaf =
    txKind === 'image' && txFilename.startsWith('tn_') ? 'thumb' : activeLeaf;

  // Engaged when the resolved file's active leaf has received at least
  // one chunk — the spacecraft has confirmed the slicing, so the count/
  // get inputs must use that exact `chunk_size`. Image leaves are
  // independent (full vs thumb can have been counted at different
  // sizes); aii/mag have a single stream per file. `null` = unlocked,
  // operator can edit freely.
  // Effective dest folder for IMG commands.
  const txDest: Dest = txDestOverride ?? (resolvedLeaf === 'thumb' ? 2 : 1);

  // Single shared node across all kinds — operator's last selection
  // persists when switching IMG/AII/MAG. Falls back to focused file's
  // source, then `baseResolvedFile`'s source (typed-filename match),
  // then 'HLNV' default.
  // txNode resolution priority:
  //   1. Provider's destNode (cross-page, persistent — shared with the
  //      Imaging page so a node picked here also flips that page).
  //   2. The focused file's source (operator just clicked a file).
  //   3. The deck-typed filename's matched-file source (rare fallback).
  //   4. HLNV default.
  const txNode: Source =
    (imageFiles.destNode as Source)
    || focused?.source
    || baseResolvedFile?.source
    || 'HLNV';

  // Refined: when there are multiple files with the same name from
  // different sources (HLNV vs ASTR), pick the one whose source
  // matches `txNode`. Falls back to the name-only base match.
  const resolvedFile: DFile | null = (() => {
    if (!baseResolvedFile) return null;
    const byNameKind = (f: DFile) => f.kind === txKind && fileName(f) === txFilename;
    const sourcePref = liveFiles.find(f => byNameKind(f) && f.source === txNode);
    if (sourcePref) return sourcePref;
    if (txKind === 'image' && txFilename.startsWith('tn_')) {
      const expectedStem = txFilename.slice(3);
      const stemMatch = liveFiles.find(
        f => f.kind === 'image' && f.stem === expectedStem && f.source === txNode,
      );
      if (stemMatch) return stemMatch;
    }
    return baseResolvedFile;
  })();

  const lockedChunkSize: number | null = (() => {
    if (!resolvedFile) return null;
    // Resolve the on-disk filename for this leaf so we can look up the
    // operator's last cnt chunk_size for it.
    let lookupFilename: string;
    let leafTotal: number;
    let leafReceived: number;
    let leafChunkSize: number;
    if (resolvedFile.kind === 'image') {
      const useThumb = resolvedLeaf === 'thumb' && hasRealThumb(resolvedFile);
      lookupFilename = useThumb ? `tn_${resolvedFile.stem}` : resolvedFile.stem;
      const leaf = useThumb ? resolvedFile.thumb! : resolvedFile.full;
      leafTotal = leaf.total;
      leafReceived = leaf.received;
      leafChunkSize = leaf.chunkSize;
    } else {
      lookupFilename = resolvedFile.filename;
      leafTotal = resolvedFile.total;
      leafReceived = resolvedFile.received;
      leafChunkSize = resolvedFile.chunkSize;
    }
    // 1) Cnt-tracked: operator's typed chunk_size. Only engage once
    //    the cnt RES has been processed for this leaf — i.e. backend
    //    `set_total` has run and propagated through `liveFiles`. The
    //    `liveLeaf` adapter coerces backend `total: null` to 0, so
    //    `leafTotal > 0` is the signal "RES received". Without this
    //    gate the lock would engage at stage time, before the
    //    spacecraft has acknowledged the cnt.
    if (leafTotal > 0) {
      const tracked = cntChunkSizes[cntKey(resolvedFile.kind, resolvedFile.source, lookupFilename)];
      if (tracked) {
        const n = parseInt(tracked, 10);
        if (Number.isFinite(n) && n > 0) return n;
      }
    }
    // 2) Backend-confirmed: feed_chunk has populated `chunk_sizes[ref]`,
    //    surfaced as `leaf.chunkSize`. Strictly stronger than #1 — chunks
    //    can't have flowed without the RES first — kept as a fallback for
    //    files restored from disk where `cntChunkSizes` was never written.
    return leafReceived > 0 ? leafChunkSize : null;
  })();

  // Mirrors legacy FilesTxControls: while the lock is engaged, force the
  // controlled input value to the locked size. When the lock releases
  // (filename cleared or local copy forgotten), the last value sticks
  // and the input becomes editable again.
  useEffect(() => {
    if (lockedChunkSize != null) setChunkSize(String(lockedChunkSize));
  }, [lockedChunkSize]);

  function setNodeForCurrentKind(n: Source) {
    // Single source of truth — the provider is shared with the Imaging
    // tab, so this also keeps that page in sync.
    imageFiles.setDestNode(n);
  }

  return (
    <div
      className="flex-1 flex flex-col overflow-hidden"
      style={{ backgroundColor: colors.bgApp, color: colors.textPrimary }}
    >
      <Topbar
        filter={filter} onFilter={setFilter}
        toolsOpen={toolsOpen} onTools={() => setToolsOpen(o => !o)}
        onCapture={() => { setCaptureOpen(true); setToolsOpen(false); }}
        onLcd={() => { setLcdOpen(true); setToolsOpen(false); }}
        onStage={(cmd) => stageCmd(cmd, {}, txNode)}
      />

      <div className="flex-1 flex overflow-hidden min-h-0 p-3 gap-3">
        {/* Left column = TX surface: command deck on top, staged queue
            beneath as its own box (operator can scan staged work
            without scrolling the deck). Both share the 400px width. */}
        <div className="shrink-0 flex flex-col gap-3 min-h-0" style={{ width: 400 }}>
          <CommandDeck
            txKind={txKind}
            txFilenameStem={txFilenameStem}
            txFilename={txFilename}
            txNode={txNode}
            txDest={txDest}
            chunkSize={chunkSize}
            lockedChunkSize={lockedChunkSize}
            resolvedFile={resolvedFile}
            resolvedLeaf={resolvedLeaf}
            pendingRestage={pendingRestage}
            schema={schema}
            onKindChange={k => { setTxKindOverride(k); setTxFilenameOverride(null); setPendingRestage(null); }}
            onFilenameChange={changeFilename}
            onNodeChange={setNodeForCurrentKind}
            onDestChange={d => setTxDestOverride(d)}
            onChunkSizeChange={setChunkSize}
            onClearRestage={() => setPendingRestage(null)}
            onDeleteRequest={() => {
              if (resolvedFile) {
                setPendingDelete({
                  file: resolvedFile,
                  scope: 'spacecraft',
                  // Active leaf's actual on-disk filename — `tn_<stem>`
                  // for thumb, plain `<stem>` for full / aii / mag.
                  targetFilename: txFilename,
                });
              }
            }}
            onStage={stageCmd}
          />
          <StagedPanel
            rows={liveStaged}
            onAbort={() => abortSend()}
            onClear={() => {
              [...liveStaged].sort((a, b) => b.index - a.index).forEach(r => removeQueueItem(r.index));
            }}
            onSendAll={() => {
              if (!txConnected) {
                showToast('TX not connected', 'error');
                return;
              }
              sendAll();
            }}
          />
        </div>
        <FocusArea
          file={focused}
          activeLeaf={activeLeaf}
          onLeafChange={l => {
            // Toggling FULL/THUMB also re-derives the deck's filename
            // input to the matching leaf name (full stem vs `tn_stem`).
            // Without clearing the override, a typed custom filename
            // would persist and mismatch the new leaf — which is what
            // made the deck look "stuck" on the previous filename.
            setActiveLeaf(l);
            setTxFilenameOverride(null);
            setPendingRestage(null);
          }}
          onPickRange={pickRangeFromChunk}
          focusedLeaf={focusedLeaf}
          focusedImageLeaf={focusedImageLeaf}
          imagePreviewVersion={imageFiles.previewVersion}
        />
        <Picker
          files={pickerFiles}
          focusedId={focusedId}
          onSelect={selectFile}
          onDelete={id => {
            const f = liveFiles.find(x => x.id === id);
            if (f) setPendingDelete({ file: f, scope: 'local' });
          }}
          search={search}
          onSearch={setSearch}
          collapsed={pickerCollapsed}
          onToggleCollapsed={() => setPickerCollapsed(c => !c)}
        />
      </div>

      <div className="px-3 pb-3 shrink-0">
        <ActivityTail
          rows={liveActivity}
          expanded={activityExpanded}
          onToggle={() => setActivityExpanded(v => !v)}
        />
      </div>

      {captureOpen && <CaptureSheet txNode={txNode} onClose={() => setCaptureOpen(false)} onStage={stageCmd} />}
      {lcdOpen     && <LcdDisplaySheet txNode={txNode} onClose={() => setLcdOpen(false)} onStage={stageCmd} />}
      <ConfirmDialog
        open={pendingDelete !== null}
        title={pendingDelete?.scope === 'local' ? 'Forget local copy?' : 'Delete on spacecraft?'}
        detail={pendingDelete
          ? (pendingDelete.scope === 'local'
              ? `Removes the ground-station's accumulated chunks for ${fileName(pendingDelete.file)} (${pendingDelete.file.source}). The on-board copy is unaffected. This cannot be undone.`
              // Spacecraft delete: name the actual on-disk filename
              // (THUMB if the operator was on the thumb leaf, FULL
              // otherwise) so a leaf switch BETWEEN del-click and
              // confirm doesn't stage the wrong leaf without the
              // operator noticing — `targetFilename` is captured at
              // click time, the dialog text echoes it verbatim.
              : (() => {
                  const tgt = pendingDelete.targetFilename ?? fileName(pendingDelete.file);
                  const leafTag = pendingDelete.file.kind === 'image'
                    ? (tgt.startsWith('tn_') ? ' [THUMB]' : ' [FULL]')
                    : '';
                  return `Stages ${fileCaps(pendingDelete.file.kind).deleteCmd} for ${tgt}${leafTag} on ${pendingDelete.file.source}. This cannot be undone.`;
                })())
          : ''}
        variant="destructive"
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (!pendingDelete) return;
          const { file, scope, targetFilename } = pendingDelete;
          if (scope === 'spacecraft') {
            // For aii/mag, the deck doesn't pass a targetFilename so
            // fall back to fileName(file) (= `f.filename` for flat
            // kinds, with extension). For images, `targetFilename` is
            // the active leaf's on-disk name (`tn_<stem>` on thumb,
            // `<stem>` on full) — staging without it would always
            // delete the FULL even when the operator is on THUMB.
            const fn = targetFilename ?? fileName(file);
            queueCommand({
              cmd_id: fileCaps(file.kind).deleteCmd,
              args: { filename: fn },
              packet: { dest: file.source },
            });
            showToast(`Staged ${fileCaps(file.kind).deleteCmd} ${fn}`, 'info');
            setPendingDelete(null);
          } else {
            void (async () => {
              try {
                // Image pairs are TWO files on disk (full + tn_*); aii/
                // mag are one. The backend's `ImagePair.stem` already
                // includes the extension (e.g. `'out.jpg'`), so use it
                // verbatim for the full leaf and prefix for the thumb.
                // Also nukes the matching `<filename>.meta.json` and
                // `.chunks/<kind>/<source>/<filename>/` per kind via
                // `store.delete_file`.
                const targets: Array<{ kind: typeof file.kind; filename: string }> =
                  file.kind === 'image'
                    ? [
                        { kind: 'image', filename: file.stem },
                        ...(file.thumb ? [{ kind: 'image' as const, filename: `tn_${file.stem}` }] : []),
                      ]
                    : [{ kind: file.kind, filename: file.filename }];
                for (const t of targets) {
                  const r = await fetch(
                    filesEndpoint('file', t.kind, t.filename, file.source),
                    { method: 'DELETE' },
                  );
                  if (!r.ok) throw new Error(`HTTP ${r.status} for ${t.filename}`);
                }
                if (file.kind === 'image') {
                  if (imageFiles.selectedId === file.id) imageFiles.setSelectedId('');
                  await imageFiles.refetch();
                } else if (file.kind === 'aii') {
                  if (aiiFiles.selectedId === file.id) aiiFiles.setSelectedId('');
                  await aiiFiles.refetch();
                } else {
                  if (magFiles.selectedId === file.id) magFiles.setSelectedId('');
                  await magFiles.refetch();
                }
                if (focusedId === file.id) setFocusedId('');
                // Drop the cnt-tracked chunk_size for every leaf we
                // just deleted — the file is gone, the lock should
                // not linger and stale-key the next file with the
                // same name.
                setCntChunkSizes(prev => {
                  const next = { ...prev };
                  for (const t of targets) {
                    delete next[cntKey(t.kind, file.source, t.filename)];
                  }
                  return next;
                });
                showToast(`Forgot local ${fileName(file)}`, 'success');
                // Only close the dialog if it still references THIS
                // delete. If the operator clicked trash on a different
                // file mid-IIFE, `pendingDelete` is now their new
                // pick — clearing it would clobber that selection.
                setPendingDelete(prev => (prev?.file.id === file.id ? null : prev));
              } catch (err) {
                // Keep the dialog open on failure so the operator
                // sees the error in context (which file failed) and
                // can retry or cancel deliberately. The toast surfaces
                // the message; the still-open dialog gives them an
                // affordance to retry or hit Cancel.
                showToast(`Local delete failed: ${(err as Error).message}`, 'error');
              }
            })();
          }
        }}
      />
    </div>
  );
}

// Standard GSS panel shell — rounded border, panel bg, optional shadow.
// All three top-level panes share this shape so the preview reads as one
// homogeneous surface rather than three different visual languages.
function PanelShell({
  children, style, className,
}: { children: React.ReactNode; style?: React.CSSProperties; className?: string }) {
  return (
    <div
      className={`flex flex-col rounded-md border overflow-hidden shadow-panel ${className ?? ''}`}
      style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel, ...style }}
    >
      {children}
    </div>
  );
}

// Standard GSS panel header — icon · 14px bold uppercase title · sub.
// Matches the FilesPage / ImagingPage panels so the preview blends in.
function PanelTitleBar({
  icon: Icon, title, sub, right,
}: { icon: typeof Send; title: string; sub?: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div
      className="flex items-center gap-2 px-3 border-b shrink-0"
      style={{
        borderColor: colors.borderSubtle,
        minHeight: 34,
        paddingTop: 6,
        paddingBottom: 6,
      }}
    >
      <Icon className="size-3.5 shrink-0" style={{ color: colors.dim }} />
      <span
        className="font-bold uppercase shrink-0"
        style={{ color: colors.value, fontSize: 14, letterSpacing: '0.02em' }}
      >
        {title}
      </span>
      {sub && <span className="text-[11px] truncate" style={{ color: colors.dim }}>{sub}</span>}
      {right}
    </div>
  );
}

// ─── TOPBAR ──────────────────────────────────────────────────────────

function Topbar({
  filter, onFilter, toolsOpen, onTools, onCapture, onLcd, onStage,
}: {
  filter: FilterKind; onFilter: (f: FilterKind) => void;
  toolsOpen: boolean; onTools: () => void;
  onCapture: () => void;
  onLcd: () => void;
  onStage: (cmd: string) => void;
}) {
  // Match the global TabStrip / GlobalHeader 30px row height. All children
  // capped at 20px so the topbar never grows past 30px regardless of
  // content (filter chip underline included).
  return (
    <div
      className="flex items-center gap-2 px-3 border-b shrink-0"
      style={{
        borderColor: colors.borderSubtle,
        backgroundColor: colors.bgPanel,
        height: 30,
      }}
    >
      <Download className="size-3.5 shrink-0" style={{ color: colors.dim }} />
      <span
        className="font-bold uppercase shrink-0"
        style={{ color: colors.value, fontSize: 13, letterSpacing: '0.02em' }}
      >
        Downlink
      </span>

      {/* Filter chips — tab-style underline, distinct from the deck's
          kind chips (which use button-fill). */}
      <div className="flex items-center self-stretch ml-2">
        {FILTERS.map(({ id, label }) => {
          const active = filter === id;
          return (
            <button
              key={id}
              onClick={() => onFilter(id)}
              className="px-2 font-mono text-[11px] btn-feedback h-full inline-flex items-center"
              style={{
                color: active ? colors.value : colors.dim,
                backgroundColor: 'transparent',
                fontWeight: active ? 600 : 400,
                letterSpacing: '0.06em',
                borderBottom: `2px solid ${active ? colors.active : 'transparent'}`,
                marginBottom: -1,
              }}
            >
              {label}
            </button>
          );
        })}
      </div>

      <div className="flex-1" />

      <div className="relative">
        <button
          onClick={onTools}
          className="inline-flex items-center gap-1 px-2 rounded-sm border font-mono text-[11px] btn-feedback shrink-0"
          style={{
            height: 20,
            color: toolsOpen ? colors.active : colors.dim,
            borderColor: toolsOpen ? colors.active : colors.borderSubtle,
            backgroundColor: toolsOpen ? colors.activeFill : 'transparent',
          }}
        >
          <MoreHorizontal className="size-3.5" />
          tools
        </button>
        {toolsOpen && (
          <div
            className="absolute right-0 top-full mt-1 z-20 flex flex-col rounded-sm border shadow-panel overflow-hidden"
            style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel, minWidth: 220 }}
          >
            <ToolItem icon={Camera}  label="Capture image..." tone={colors.active} onClick={onCapture} />
            <ToolDivider label="LCD" />
            <ToolItem icon={Power}   label="lcd_on"           tone={colors.success} onClick={() => { onStage('lcd_on');    onTools(); }} />
            <ToolItem icon={Power}   label="lcd_off"          tone={colors.dim}     onClick={() => { onStage('lcd_off');   onTools(); }} />
            <ToolItem icon={Monitor} label="lcd_display..."   tone={colors.active}  onClick={onLcd} />
            <ToolItem icon={Eraser}  label="lcd_clear"        tone={colors.warning} onClick={() => { onStage('lcd_clear'); onTools(); }} />
            <ToolDivider label="Camera" />
            <ToolItem icon={Power}   label="cam_on"           tone={colors.success} onClick={() => { onStage('cam_on');  onTools(); }} />
            <ToolItem icon={Power}   label="cam_off"          tone={colors.dim}     onClick={() => { onStage('cam_off'); onTools(); }} />
            <ToolDivider label="Magnetometer" />
            {/* mag_capture lives in MAG StandaloneOps with safe defaults
                (filename/time/mode); the toolbar previously staged it
                with no args, which the schema rejects. */}
            <ToolItem icon={Radio}   label="mag_kill"         tone={colors.danger}  onClick={() => { onStage('mag_kill');    onTools(); }} />
            <ToolItem icon={Radio}   label="mag_tlm"          tone={colors.dim}     onClick={() => { onStage('mag_tlm');     onTools(); }} />
          </div>
        )}
      </div>
    </div>
  );
}
function ToolItem({ icon: Icon, label, tone, onClick }: { icon: typeof Camera; label: string; tone: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-2 px-3 py-1.5 text-[11px] font-mono hover:bg-white/[0.04] text-left"
      style={{ color: colors.textPrimary }}
    >
      <Icon className="size-3" style={{ color: tone }} />
      {label}
    </button>
  );
}
function ToolDivider({ label }: { label: string }) {
  return (
    <div
      className="px-3 pt-2 pb-1 text-[11px] uppercase tracking-wider font-semibold"
      style={{ color: colors.sep, borderTop: `1px solid ${colors.borderSubtle}` }}
    >
      {label}
    </div>
  );
}

// ─── PICKER ──────────────────────────────────────────────────────────
// Width chosen to fit ~24-char filenames without truncation; collapsible
// when the operator wants more room for focus + deck. Search lives here
// (not the topbar) so picker filtering reads as one local UI block.

const PICKER_WIDTH_EXPANDED = 260;
const PICKER_WIDTH_COLLAPSED = 28;

function Picker({
  files, focusedId, onSelect, onDelete,
  search, onSearch, collapsed, onToggleCollapsed,
}: {
  files: DFile[];
  focusedId: string;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  search: string;
  onSearch: (s: string) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  if (collapsed) {
    // Picker lives on the RIGHT side of the page, so the expand chevron
    // points LEFT (toward the focus area where the picker grows into)
    // and the collapse chevron in the expanded state points RIGHT.
    return (
      <PanelShell className="shrink-0" style={{ width: PICKER_WIDTH_COLLAPSED }}>
        <button
          onClick={onToggleCollapsed}
          className="flex items-center justify-center btn-feedback shrink-0 border-b"
          style={{ height: 34, color: colors.dim, borderColor: colors.borderSubtle }}
          title="Expand file picker"
        >
          <ChevronLeft className="size-3.5" />
        </button>
        <button
          onClick={onToggleCollapsed}
          className="flex-1 flex items-center justify-center btn-feedback"
          title={`Expand file picker (${files.length} file${files.length === 1 ? '' : 's'})`}
          style={{ color: colors.dim }}
        >
          <span
            className="font-bold uppercase font-mono"
            style={{ writingMode: 'vertical-rl', letterSpacing: '0.1em', fontSize: 11 }}
          >
            Files · {files.length}
          </span>
        </button>
      </PanelShell>
    );
  }
  return (
    <PanelShell className="shrink-0" style={{ width: PICKER_WIDTH_EXPANDED }}>
      <PanelTitleBar
        icon={FileBox}
        title="Files"
        sub={`${files.length}`}
        right={
          <button
            onClick={onToggleCollapsed}
            className="ml-auto inline-flex items-center justify-center btn-feedback shrink-0"
            style={{ width: 20, height: 20, color: colors.dim }}
            title="Collapse file picker"
          >
            <ChevronRight className="size-3.5" />
          </button>
        }
      />
      <div
        className="px-2 py-1.5 border-b shrink-0"
        style={{ borderColor: colors.borderSubtle }}
      >
        <div className="relative flex items-center">
          <Search className="absolute left-2 size-3.5 pointer-events-none" style={{ color: colors.dim }} />
          <input
            className="w-full pl-7 pr-7 font-mono text-[11px] rounded-sm border outline-none"
            style={{ height: 22, backgroundColor: colors.bgApp, borderColor: colors.borderSubtle, color: colors.textPrimary }}
            placeholder="search filename..."
            value={search}
            onChange={e => onSearch(e.target.value)}
          />
          {search && (
            <button
              onClick={() => onSearch('')}
              className="absolute right-1 inline-flex items-center justify-center"
              style={{ width: 18, height: 18, color: colors.dim }}
              title="clear search"
            >
              <X className="size-3" />
            </button>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-auto py-1 min-h-0">
        {files.map(f => (
          <PickerRow
            key={f.id}
            file={f}
            focused={f.id === focusedId}
            onSelect={() => onSelect(f.id)}
            onDelete={() => onDelete(f.id)}
          />
        ))}
        {files.length === 0 && (
          <div className="px-3 py-8 text-center italic text-[11px]" style={{ color: colors.textMuted }}>
            {search ? 'no matches' : 'no files'}
          </div>
        )}
      </div>
    </PanelShell>
  );
}

function PickerRow({
  file, focused, onSelect, onDelete,
}: { file: DFile; focused: boolean; onSelect: () => void; onDelete: () => void }) {
  const tot = aggregateTotals(file);
  const state = fileOverallState(file);
  const isPair = file.kind === 'image' && hasRealThumb(file);
  const tone = STATE_TONE[state];
  // State symbol — duplicates the color encoding for HFDS 9.3.6
  // accessibility (every color-coded status also gets a non-color cue).
  const stateSymbol =
    state === 'discovered' ? '?'
    : state === 'counted'  ? '·'
    : state === 'complete' ? '✓'
    : '▶';
  const stateValue =
    state === 'discovered' ? '—'
    : state === 'counted'  ? '0%'
    : state === 'complete' ? ''
    : `${tot.pct}%`;
  return (
    <div
      className="group w-full flex items-center gap-1.5 px-2 py-1 hover:bg-white/[0.03] transition-colors"
      style={{
        backgroundColor: focused ? colors.bgPanelRaised : 'transparent',
        // Selection rail uses borderStrong (neutral) instead of active
        // cyan so it doesn't visually compete with kind-glyph cyan and
        // the in-flight state tint.
        borderLeft: `3px solid ${focused ? colors.borderStrong : 'transparent'}`,
        paddingLeft: focused ? 5 : 8,
        minHeight: 24,
      }}
    >
      <button
        onClick={onSelect}
        className="flex-1 min-w-0 flex items-center gap-1.5 text-left outline-none"
      >
        <KindGlyph kind={file.kind} />
        <span
          className="font-mono text-[11px] uppercase tracking-wider shrink-0"
          style={{ color: colors.dim, letterSpacing: '0.06em' }}
          title={`Source: ${file.source}`}
        >
          {file.source}
        </span>
        <span
          className="font-mono text-[11px] truncate flex-1 min-w-0"
          style={{
            color: state === 'complete' ? colors.value : colors.textPrimary,
            fontWeight: focused ? 600 : 400,
          }}
          title={fileName(file)}
        >
          {fileName(file)}
        </span>
        {isPair && (
          <span
            className="font-mono text-[11px] shrink-0"
            style={{ color: colors.success }}
            title="paired with thumbnail"
          >
            +tn
          </span>
        )}
        <span
          className={`font-mono shrink-0 ${state === 'in-flight' ? 'animate-pulse-text' : ''}`}
          style={{ color: tone, fontSize: 11, width: 10, textAlign: 'center' }}
          title={`state: ${state}`}
        >
          {stateSymbol}
        </span>
        {stateValue && (
          <span
            className="text-[11px] tabular-nums font-mono shrink-0"
            style={{ color: tone, minWidth: 28, textAlign: 'right' }}
          >
            {stateValue}
          </span>
        )}
      </button>
      {/* Hover-revealed delete affordance — matches FilesTable's per-row
          delete pattern. Click stops propagation so the row doesn't also
          select. */}
      <button
        onClick={(e) => { e.stopPropagation(); onDelete(); }}
        className="shrink-0 inline-flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
        style={{ width: 18, height: 18, color: colors.danger }}
        title={`Delete ${fileName(file)}`}
      >
        <Trash2 className="size-3" />
      </button>
    </div>
  );
}

function KindGlyph({ kind }: { kind: Kind }) {
  if (kind === 'image') return <ImageIcon className="size-3.5" style={{ color: colors.active }} />;
  if (kind === 'aii')   return <FileJson  className="size-3.5" style={{ color: colors.success }} />;
  return <FileBox className="size-3.5" style={{ color: colors.neutral }} />;
}

// SourceDot was removed during the color audit — kind/state tones already
// distinguish files; source is shown inline as plain text in the focus
// header. Keeping the type alias `Source` since data still tracks it.

function ProgressBar({ pct, complete, fresh }: { pct: number; complete: boolean; fresh?: boolean }) {
  // Tone semantics:
  //   complete → success (green)
  //   pct=0    → neutral (nothing yet, not a failure)
  //   else     → info    (in-flight, sending/receiving)
  const tone = complete ? colors.success : pct === 0 ? colors.neutral : colors.info;
  return (
    <div
      className="rounded-full overflow-hidden flex-1 relative"
      style={{ height: 4, backgroundColor: `${tone}1F` }}
    >
      <div
        style={{
          width: `${Math.max(2, pct)}%`,
          height: '100%',
          backgroundColor: tone,
          transition: 'width 240ms ease',
          boxShadow: fresh ? `0 0 6px ${tone}88` : undefined,
        }}
      />
    </div>
  );
}

// ─── FOCUS AREA ──────────────────────────────────────────────────────

function FocusArea({
  file, focusedLeaf, focusedImageLeaf, imagePreviewVersion,
  activeLeaf, onLeafChange, onPickRange,
}: {
  file: DFile | null;
  focusedLeaf: FileLeaf | null;
  focusedImageLeaf: FileLeaf | null;
  imagePreviewVersion: string;
  activeLeaf: Leaf;
  onLeafChange: (l: Leaf) => void;
  onPickRange: (start: number, count: number) => void;
}) {
  if (!file) {
    return (
      <PanelShell className="flex-1 min-w-0">
        <PanelTitleBar icon={ImageIcon} title="Preview" sub="no file focused" />
        <FocusEmpty />
      </PanelShell>
    );
  }
  return (
    <PanelShell className="flex-1 min-w-0">
      <FocusHeader file={file} activeLeaf={activeLeaf} onLeafChange={onLeafChange} />
      {file.kind === 'image' ? (
        <ImageFocus
          file={file}
          activeLeaf={activeLeaf}
          imageLeaf={focusedImageLeaf}
          imagePreviewVersion={imagePreviewVersion}
          onPickRange={onPickRange}
        />
      ) : file.kind === 'aii' ? (
        <AiiFocus file={file} leaf={focusedLeaf} onPickRange={onPickRange} />
      ) : (
        <MagFocus file={file} leaf={focusedLeaf} onPickRange={onPickRange} />
      )}
    </PanelShell>
  );
}

function FocusEmpty() {
  return (
    <div className="flex-1 flex flex-col items-center justify-center" style={{ color: colors.dim }}>
      <FileBox className="size-10 mb-3 opacity-50" />
      <div className="text-[12px] font-mono" style={{ letterSpacing: '0.04em' }}>
        focus a file from the picker
      </div>
    </div>
  );
}

function FocusHeader({
  file, activeLeaf, onLeafChange,
}: {
  file: DFile;
  activeLeaf: Leaf;
  onLeafChange: (l: Leaf) => void;
}) {
  const leaf = leafTotals(file, activeLeaf);
  const showLeafToggle = file.kind === 'image' && hasRealThumb(file as ImageFile);
  // Filename is the title (kind tone via KindGlyph; source as plain text;
  // counts as sub). For images-with-thumb, an inline FULL/THUMB toggle
  // sits next to the kind glyph — same shape the legacy
  // imaging/PreviewPanel uses (Tabs in the panel header).
  return (
    <div
      className="flex items-center gap-2 px-3 border-b shrink-0"
      style={{
        borderColor: colors.borderSubtle,
        minHeight: 34,
        paddingTop: 6,
        paddingBottom: 6,
        overflow: 'hidden',
      }}
    >
      <KindGlyph kind={file.kind} />
      {showLeafToggle && (
        <LeafToggleInline file={file as ImageFile} activeLeaf={activeLeaf} onLeafChange={onLeafChange} />
      )}
      <span
        className="font-bold shrink-0 truncate"
        style={{ color: colors.value, fontSize: 14, letterSpacing: '0.02em' }}
        title={leafFilename(file, activeLeaf)}
      >
        {leafFilename(file, activeLeaf)}
      </span>
      <span className="text-[11px] font-mono truncate" style={{ color: colors.dim }}>
        {file.source}
        {' · '}
        {leaf.total === 0 ? (
          <span style={{ color: colors.info }}>count required</span>
        ) : (
          <>
            <span className="tabular-nums" style={{ color: colors.value }}>
              {leaf.received}/{leaf.total}
            </span>
            {` chunks · ${leaf.chunkSize} B`}
          </>
        )}
      </span>
    </div>
  );
}

// Compact FULL/THUMB toggle that lives inline in the FocusHeader, like
// the legacy `imaging/PreviewPanel`'s Tabs. Tones match
// shared/FilenameInput.tsx:
//   FULL  → active  (cyan, primary leaf)
//   THUMB → warning (yellow, secondary/guarded leaf)
function LeafToggleInline({
  file, activeLeaf, onLeafChange,
}: { file: ImageFile; activeLeaf: Leaf; onLeafChange: (l: Leaf) => void }) {
  return (
    <div
      className="flex items-center gap-px rounded-sm overflow-hidden shrink-0"
      style={{ border: `1px solid ${colors.borderSubtle}`, backgroundColor: colors.bgApp }}
    >
      <LeafButton
        active={activeLeaf === 'full'}
        tone={colors.active}
        label="FULL"
        onClick={() => onLeafChange('full')}
      />
      <LeafButton
        active={activeLeaf === 'thumb'}
        tone={colors.warning}
        label="THUMB"
        disabled={!file.thumb}
        onClick={() => file.thumb && onLeafChange('thumb')}
      />
    </div>
  );
}

function LeafButton({
  active, tone, label, disabled, onClick,
}: { active: boolean; tone: string; label: string; disabled?: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="px-2 font-mono text-[11px] btn-feedback disabled:opacity-40"
      style={{
        height: 20,
        color: active ? colors.bgApp : colors.dim,
        backgroundColor: active ? tone : 'transparent',
        fontWeight: active ? 600 : 400,
        letterSpacing: '0.06em',
      }}
    >
      {label}
    </button>
  );
}

// StateBadge was removed during the audit pass — the focus header's
// chunk-count sub already conveys state, and the picker uses a tinted
// percentage. STATE_LABEL/STATE_TONE remain as data for the picker dot.

// — IMAGE FOCUS — leaf-aware -----------------------------------------

function ImageFocus({
  file, activeLeaf, imageLeaf, imagePreviewVersion, onPickRange,
}: {
  file: ImageFile;
  activeLeaf: Leaf;
  imageLeaf: FileLeaf | null;
  imagePreviewVersion: string;
  onPickRange: (start: number, count: number) => void;
}) {
  const leaf = leafTotals(file, activeLeaf);
  // Target for the missing-range fetch — the resolved per-leaf FileLeaf
  // carries the right (full vs thumb) filename, source, and progress
  // counters.
  const target: ChunkSetTarget | null = imageLeaf
    ? {
        kind: 'image',
        filename: imageLeaf.filename,
        source: imageLeaf.source,
        total: imageLeaf.total,
        received: imageLeaf.received,
      }
    : null;
  return (
    <div className="flex-1 grid grid-rows-[1fr_auto] min-h-0 overflow-auto p-5 gap-4">
      <ProgressivePreview
        leaf={imageLeaf}
        version={imagePreviewVersion}
      />
      <ChunkTimeline
        leaf={leaf}
        target={target}
        tone={activeLeaf === 'thumb' ? colors.warning : colors.active}
        onPickRange={onPickRange}
      />
    </div>
  );
}

function ProgressivePreview({
  leaf, version,
}: {
  leaf: FileLeaf | null;
  version: string;
}) {
  // Fetch the (partially-)assembled JPEG from the preview endpoint;
  // version cache-busts so the <img> reloads on every chunk arrival.
  // Matches legacy `imaging/PreviewPanel`:
  //   • before chunk 0 lands → 404 → onError silently swallows
  //   • partial assembly → truncated JPEG (browsers decode top-down,
  //     so the image grows visibly as chunks land)
  //   • complete + meta on disk → fully assembled image
  // Just the <img> — no border/background chrome, no mask, no progress
  // overlay. Progress lives in the ChunkTimeline below.
  const imgSrc = useMemo(() => {
    if (!leaf) return '';
    const endpoint = filesEndpoint('preview', 'image', leaf.filename, leaf.source);
    const sep = endpoint.includes('?') ? '&' : '?';
    return `${endpoint}${sep}v=${encodeURIComponent(String(version))}`;
  }, [leaf, version]);
  if (!imgSrc) return <div className="flex-1 min-h-0" />;
  return (
    <div className="flex-1 min-h-0 relative">
      <img
        src={imgSrc}
        alt={leaf?.filename ?? ''}
        className="absolute inset-0 w-full h-full object-contain"
        onError={() => {}}
      />
    </div>
  );
}

const MAX_RANGE_CHIPS = 8;

function ChunkTimeline({
  leaf, target, tone, onPickRange,
}: {
  leaf: LeafData;
  /** Identity for the received-chunk index fetch. `null` when there's
   *  no resolved file (e.g. AII/MAG with `focusedLeaf=null`). */
  target: ChunkSetTarget | null;
  tone: string;
  onPickRange?: (start: number, count: number) => void;
}) {
  const complete = leaf.received === leaf.total && leaf.total > 0;
  const noTotal = leaf.total === 0;
  // Real received-chunk indices from /api/plugins/files/chunks. The hook
  // returns an empty Set when target is null or total is unknown, which
  // collapses to `ranges = []` below and is exactly the right behavior
  // (nothing to surface as "missing" when we don't even have a count).
  const receivedSet = useFileChunkSet(target);
  const ranges = noTotal
    ? []
    : computeMissingRanges(leaf.total, receivedSet).map(
        r => [r.start, r.end] as [number, number],
      );
  const visibleRanges = ranges.slice(0, MAX_RANGE_CHIPS);
  const hiddenCount = ranges.length - visibleRanges.length;
  const interactive = !!onPickRange && !complete && !noTotal;
  return (
    <div
      className="rounded-md border p-3"
      style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}
    >
      {/* Header row removed — label (FULL/THUMB) is in the FocusHeader's
          leaf toggle; received/total + state already show in the
          FocusHeader's chunk-count sub. The bar's fill + tone covers
          progress, and the missing-range chips below carry the
          actionable info. */}
      <div
        className="relative w-full rounded-sm overflow-hidden"
        style={{
          height: 16,
          backgroundColor: complete ? `${colors.success}22` : noTotal ? `${colors.dim}1A` : `${tone}1A`,
          border: `1px solid ${complete ? `${colors.success}55` : noTotal ? `${colors.dim}55` : `${tone}55`}`,
          backgroundImage: noTotal
            ? `repeating-linear-gradient(45deg, ${colors.dim}22 0 4px, transparent 4px 8px)`
            : undefined,
        }}
      >
        {!complete && !noTotal && (
          <div
            style={{
              position: 'absolute', inset: 0,
              width: `${pct(leaf.received, leaf.total)}%`,
              backgroundColor: tone,
              boxShadow: `0 0 8px ${tone}66`,
              transition: 'width 240ms ease',
            }}
          />
        )}
        {complete && (
          <div style={{ position: 'absolute', inset: 0, backgroundColor: colors.success }} />
        )}
        {ranges.map(([lo, hi], i) => {
          const left  = (lo / leaf.total) * 100;
          const width = ((hi - lo + 1) / leaf.total) * 100;
          return (
            <div
              key={i}
              style={{
                position: 'absolute',
                left: `${left}%`,
                width: `${width}%`,
                top: 0, bottom: 0,
                background: `repeating-linear-gradient(45deg, ${colors.danger}55 0 4px, transparent 4px 8px)`,
                borderLeft:  `1px solid ${colors.danger}99`,
                borderRight: `1px solid ${colors.danger}99`,
              }}
              title={`missing ${lo}–${hi}`}
            />
          );
        })}
      </div>

      {!complete && !noTotal && ranges.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5 font-mono text-[11px] items-center">
          <span style={{ color: colors.dim }}>missing:</span>
          {visibleRanges.map(([lo, hi], i) => {
            const count = hi - lo + 1;
            return (
              <button
                key={i}
                onClick={interactive ? () => onPickRange!(lo, count) : undefined}
                disabled={!interactive}
                className="px-1.5 rounded-sm tabular-nums btn-feedback hover:brightness-150"
                style={{
                  color: colors.danger,
                  backgroundColor: colors.dangerFill,
                  border: `1px solid ${colors.danger}55`,
                  cursor: interactive ? 'pointer' : 'default',
                }}
                title={interactive ? `click to stage missing range ${lo}–${hi} (${count} chunks)` : `missing ${lo}–${hi}`}
              >
                {lo}–{hi}
              </button>
            );
          })}
          {hiddenCount > 0 && (
            <span
              className="px-1.5 rounded-sm tabular-nums"
              style={{ color: colors.dim, backgroundColor: colors.neutralFill, border: `1px solid ${colors.borderSubtle}` }}
            >
              +{hiddenCount} more
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// — AII FOCUS ---------------------------------------------------------

function AiiFocus({
  file, leaf, onPickRange,
}: {
  file: FlatFile;
  leaf: FileLeaf | null;
  onPickRange: (start: number, count: number) => void;
}) {
  const complete = file.total > 0 && file.received === file.total;
  const target: ChunkSetTarget | null = leaf
    ? { kind: 'aii', filename: leaf.filename, source: leaf.source, total: leaf.total, received: leaf.received }
    : null;
  return (
    <div className="flex-1 grid grid-rows-[auto_1fr] min-h-0 overflow-auto p-5 gap-4">
      <ChunkTimeline
        leaf={{ received: file.received, total: file.total, chunkSize: file.chunkSize }}
        target={target}
        tone={complete ? colors.success : colors.info}
        onPickRange={onPickRange}
      />
      {/* Reuse the existing JsonPreview — fetches /api/plugins/files/preview
          and pretty-prints. Drops the local AII_RAW mock + parse logic. */}
      <div className="rounded-md border overflow-hidden" style={{ borderColor: colors.borderSubtle }}>
        <JsonPreview file={leaf} />
      </div>
    </div>
  );
}

// — MAG FOCUS — reuse existing MagPreview metadata + download anchor.

function MagFocus({
  file, leaf, onPickRange,
}: {
  file: FlatFile;
  leaf: FileLeaf | null;
  onPickRange: (start: number, count: number) => void;
}) {
  const complete = file.total > 0 && file.received === file.total;
  const target: ChunkSetTarget | null = leaf
    ? { kind: 'mag', filename: leaf.filename, source: leaf.source, total: leaf.total, received: leaf.received }
    : null;
  return (
    <div className="flex-1 grid grid-rows-[auto_1fr] min-h-0 overflow-auto p-5 gap-4">
      <ChunkTimeline
        leaf={{ received: file.received, total: file.total, chunkSize: file.chunkSize }}
        target={target}
        tone={complete ? colors.success : colors.info}
        onPickRange={onPickRange}
      />
      <div className="rounded-md border overflow-hidden" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
        <MagPreview file={leaf} />
      </div>
    </div>
  );
}

// ─── COMMAND DECK ────────────────────────────────────────────────────
// Two layers of input:
//   1. Kind chips (always visible) — pick IMG/AII/MAG.
//   2. Filename input — auto-fills from focus, but freely editable so the
//      operator can prep a not-yet-known file (cnt → first chunk creates
//      the entry).
// Picker selection just SEEDS these. Override at any time; ↻ resets.

function CommandDeck({
  txKind, txFilenameStem, txFilename, txNode, txDest, chunkSize, lockedChunkSize, resolvedFile, resolvedLeaf,
  pendingRestage,
  schema,
  onKindChange, onFilenameChange, onNodeChange, onDestChange, onChunkSizeChange, onClearRestage,
  onDeleteRequest,
  onStage,
}: {
  txKind: Kind;
  txFilenameStem: string;
  txFilename: string;
  txNode: Source;
  txDest: Dest;
  chunkSize: string;
  lockedChunkSize: number | null;
  resolvedFile: DFile | null;
  resolvedLeaf: Leaf;
  pendingRestage: { start: number; count: number; key: number } | null;
  schema: Record<string, Record<string, unknown>> | null;
  onKindChange: (k: Kind) => void;
  onFilenameChange: (stem: string) => void;
  onNodeChange: (n: Source) => void;
  onDestChange: (d: Dest) => void;
  onChunkSizeChange: (v: string) => void;
  onClearRestage: () => void;
  onDeleteRequest: () => void;
  onStage: (cmd_id: string, args?: Record<string, string>, dest?: string) => void;
}) {
  return (
    <PanelShell className="flex-1 min-h-0">
      <PanelTitleBar
        icon={Send}
        title="Command"
        sub={`↳ ${txNode}`}
      />
      <div
        className="shrink-0 border-b"
        style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgApp }}
      >
        <KindNodeRow txKind={txKind} txNode={txNode} onKindChange={onKindChange} onNodeChange={onNodeChange} />
        <FilenameRow txKind={txKind} stem={txFilenameStem} onChange={onFilenameChange} resolved={resolvedFile} />
        {txKind === 'image' && (
          <DestRow dest={txDest} onChange={onDestChange} />
        )}
        <ChunkSizeRow value={chunkSize} onChange={onChunkSizeChange} lockedSize={lockedChunkSize} />
      </div>
      <div className="flex-1 overflow-auto p-3 space-y-3 min-h-0">
        <PrimaryActionBlock
          txKind={txKind}
          txFilename={txFilename}
          txNode={txNode}
          txDest={txDest}
          chunkSize={chunkSize}
          resolvedFile={resolvedFile}
          resolvedLeaf={resolvedLeaf}
          onStage={onStage}
        />
        <RangeBlock
          txKind={txKind}
          txFilename={txFilename}
          txNode={txNode}
          txDest={txDest}
          chunkSize={chunkSize}
          resolvedFile={resolvedFile}
          resolvedLeaf={resolvedLeaf}
          pendingRestage={pendingRestage}
          onClearRestage={onClearRestage}
          onStage={onStage}
        />
        <SecondaryActions
          txKind={txKind}
          txFilename={txFilename}
          txNode={txNode}
          txDest={txDest}
          chunkSize={chunkSize}
          disabled={!txFilename}
          onStage={onStage}
          onDeleteRequest={onDeleteRequest}
        />
        <StandaloneOps kind={txKind} txNode={txNode} schema={schema} onStage={onStage} />
      </div>
    </PanelShell>
  );
}

// Standalone bordered panel for the staged TX queue. Pulled out of
// CommandDeck so the operator can scan staged work without scrolling
// past the command-builder controls. Capped height with internal
// scroll keeps it from elbowing the deck out of the column.
function StagedPanel({
  rows, onAbort, onClear, onSendAll,
}: {
  rows: ReadonlyArray<StagedRow>;
  onAbort: () => void;
  onClear: () => void;
  onSendAll: () => void;
}) {
  return (
    <PanelShell className="shrink-0" style={{ maxHeight: 280 }}>
      <PanelTitleBar
        icon={Send}
        title="Staged"
        sub={rows.length === 0 ? 'empty' : `${rows.length} queued`}
      />
      <div className="flex-1 overflow-auto p-3 min-h-0">
        <StagedSection
          rows={rows}
          onAbort={onAbort}
          onClear={onClear}
          onSendAll={onSendAll}
        />
      </div>
    </PanelShell>
  );
}

// On-board folder destination for IMG commands. Operator-pickable; the
// active leaf seeds a default but operator overrides for prestored (0)
// or to inspect the other leaf without changing the focus. Hidden for
// AII/MAG since they don't take a destination arg.
function DestRow({ dest, onChange }: { dest: Dest; onChange: (d: Dest) => void }) {
  return (
    <div
      className="flex items-center gap-2 px-3 py-1.5 shrink-0"
    >
      <span className="text-[11px] uppercase tracking-wider font-semibold" style={{ color: colors.dim }}>dest</span>
      <div
        className="flex items-center gap-px rounded-sm overflow-hidden"
        style={{ border: `1px solid ${colors.borderSubtle}` }}
      >
        {DEST_OPTIONS.map(opt => {
          const active = dest === opt.id;
          return (
            <button
              key={opt.id}
              onClick={() => onChange(opt.id)}
              title={opt.title}
              className="px-2.5 font-mono text-[11px] btn-feedback"
              style={{
                height: 22,
                color: active ? colors.bgBase : colors.dim,
                backgroundColor: active ? colors.active : 'transparent',
                fontWeight: active ? 600 : 400,
                letterSpacing: '0.06em',
              }}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Combined kind + node row. Two pill groups, single label per group, no
// right-side hint text (removed in audit pass — hints repeat after first
// read).
function KindNodeRow({
  txKind, txNode, onKindChange, onNodeChange,
}: {
  txKind: Kind;
  txNode: Source;
  onKindChange: (k: Kind) => void;
  onNodeChange: (n: Source) => void;
}) {
  return (
    <div
      className="flex items-center gap-3 px-3 py-1.5 shrink-0"
      style={{ backgroundColor: colors.bgApp }}
    >
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] uppercase tracking-wider font-semibold" style={{ color: colors.dim }}>kind</span>
        <div
          className="flex items-center gap-px rounded-sm overflow-hidden"
          style={{ border: `1px solid ${colors.borderSubtle}` }}
        >
          {KINDS.map(k => {
            const active = k === txKind;
            const tone = KIND_TONE[k];
            return (
              <button
                key={k}
                onClick={() => onKindChange(k)}
                className="px-2.5 font-mono text-[11px] btn-feedback"
                style={{
                  height: 22,
                  color: active ? colors.bgBase : colors.dim,
                  backgroundColor: active ? tone : 'transparent',
                  fontWeight: active ? 600 : 400,
                  letterSpacing: '0.06em',
                }}
              >
                {KIND_LABEL[k]}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex items-center gap-1.5">
        <span className="text-[11px] uppercase tracking-wider font-semibold" style={{ color: colors.dim }}>node</span>
        <div
          className="flex items-center gap-px rounded-sm overflow-hidden"
          style={{ border: `1px solid ${colors.borderSubtle}` }}
        >
          {(['HLNV', 'ASTR'] as const).map(n => {
            const active = txNode === n;
            return (
              <button
                key={n}
                onClick={() => onNodeChange(n)}
                className="px-2.5 font-mono text-[11px] btn-feedback"
                style={{
                  height: 22,
                  color: active ? colors.bgBase : colors.dim,
                  backgroundColor: active ? colors.active : 'transparent',
                  fontWeight: active ? 600 : 400,
                  letterSpacing: '0.06em',
                }}
              >
                {n}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function FilenameRow({
  txKind, stem, onChange, resolved,
}: { txKind: Kind; stem: string; onChange: (stem: string) => void; resolved: DFile | null }) {
  const ext = KIND_EXT[txKind];
  // Compact one-line status hint — single-word states. Verbose explanation
  // ("count first") was redundant: the primary action button already says
  // "Count chunks" when the file is novel.
  const status =
    !stem ? { tone: colors.dim,     dot: colors.neutral, text: 'no target' }
    : resolved ? { tone: colors.success, dot: colors.success, text: 'known' }
    : { tone: colors.warning, dot: colors.warning, text: 'novel' };
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 shrink-0">
      <span className="text-[11px] uppercase tracking-wider font-semibold shrink-0" style={{ color: colors.dim }}>
        file
      </span>
      <div className="relative flex-1">
        <input
          value={stem}
          onChange={e => onChange(e.target.value)}
          placeholder={txKind === 'image' ? 'capture_009 or tn_capture_009' : txKind === 'aii' ? 'transmit_dir' : 'a'}
          className="w-full pr-12 px-2 font-mono rounded-sm border outline-none text-[11px]"
          style={{
            height: 24,
            backgroundColor: colors.bgApp,
            borderColor: status.tone === colors.dim ? colors.borderSubtle : `${status.tone}55`,
            color: colors.textPrimary,
          }}
        />
        <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[11px] font-mono pointer-events-none" style={{ color: colors.dim }}>
          {ext}
        </span>
      </div>
      <span
        className="rounded-full shrink-0"
        style={{ width: 6, height: 6, backgroundColor: status.dot }}
        title={status.text}
      />
      <span
        className="text-[11px] font-mono shrink-0 truncate"
        style={{ color: status.tone, maxWidth: 160 }}
      >
        {status.text}
      </span>
    </div>
  );
}

// State-aware primary action — driven by (txKind, txFilename, resolvedFile, leaf).
// When the filename doesn't resolve to a known file, primary is always
// "Count chunks" — the cnt command paired with first-chunk arrival is
// what registers a new file in GSS state.
function PrimaryActionBlock({
  txKind, txFilename, txNode, txDest, chunkSize, resolvedFile, resolvedLeaf, onStage,
}: {
  txKind: Kind;
  txFilename: string;
  txNode: Source;
  txDest: Dest;
  chunkSize: string;
  resolvedFile: DFile | null;
  resolvedLeaf: Leaf;
  onStage: (cmd_id: string, args?: Record<string, string>, dest?: string) => void;
}) {
  const tone = KIND_TONE[txKind];
  const noFilename = !txFilename;
  const caps = fileCaps(txKind);
  const stagedName = txFilename ? withExtension(txFilename, txKind) : '';

  // Real received-chunk Set for the resolved file's active leaf.
  // Same hook ChunkTimeline uses; React-internal cache + abort race
  // guard means parallel callers don't double-fetch. Drives the
  // first-gap resume so "Pull N missing" stages chunks the operator
  // actually doesn't have, not a naive contiguous tail starting at
  // `received` (which lands on the wrong index for out-of-order RX).
  const inFlightTarget: ChunkSetTarget | null = (resolvedFile && stagedName)
    ? {
        kind: txKind,
        filename: stagedName,
        source: txNode,
        total: resolvedFile.kind === 'image'
          ? leafTotals(resolvedFile, resolvedLeaf).total
          : resolvedFile.total,
        received: resolvedFile.kind === 'image'
          ? leafTotals(resolvedFile, resolvedLeaf).received
          : resolvedFile.received,
      }
    : null;
  const receivedSet = useFileChunkSet(inFlightTarget);

  let label: string;
  let cmd: string;
  let args: Record<string, string> = {};
  let disabled = false;
  let stateMsg: string;

  if (noFilename) {
    label = 'Count chunks';
    cmd = caps.cntCmd;
    stateMsg = 'enter a filename above first';
    disabled = true;
  } else if (!resolvedFile) {
    label = 'Count chunks';
    cmd = caps.cntCmd;
    args = { filename: stagedName, chunk_size: chunkSize };
    if (caps.hasDestinationArg) args.destination = String(txDest);
    stateMsg = 'novel filename — count to register and learn the total';
  } else {
    const leaf = leafTotals(resolvedFile, resolvedLeaf);
    const state = leafState(leaf);
    if (state === 'discovered') {
      label = 'Count chunks';
      cmd = caps.cntCmd;
      args = { filename: stagedName, chunk_size: chunkSize };
      if (caps.hasDestinationArg) args.destination = String(txDest);
      stateMsg = 'total unknown — start by counting';
    } else if (state === 'counted') {
      label = `Get all ${leaf.total} chunks`;
      cmd = caps.getCmd;
      args = {
        filename: stagedName,
        start_chunk: '0',
        num_chunks: String(leaf.total),
        chunk_size: chunkSize,
      };
      if (caps.hasDestinationArg) args.destination = String(txDest);
      stateMsg = `${leaf.total} chunks counted, none received`;
    } else if (state === 'in-flight') {
      const missing = leaf.total - leaf.received;
      // Stage the FIRST contiguous missing range (computed from the
      // real received-index Set), not a naive tail from `received`.
      // Out-of-order RF arrivals leave gaps before the high-water
      // mark; resuming from `received` over-fetches already-received
      // chunks. For non-contiguous gaps the operator can still click
      // a specific range chip in the timeline to stage that gap.
      const ranges = computeMissingRanges(leaf.total, receivedSet);
      const firstGap = ranges[0] ?? { start: leaf.received, count: missing };
      label = `Pull ${firstGap.count} missing`;
      cmd = caps.getCmd;
      args = {
        filename: stagedName,
        start_chunk: String(firstGap.start),
        num_chunks: String(firstGap.count),
        chunk_size: chunkSize,
      };
      if (caps.hasDestinationArg) args.destination = String(txDest);
      const moreNote = ranges.length > 1 ? ` · ${ranges.length - 1} more gap${ranges.length > 2 ? 's' : ''}` : '';
      stateMsg = `${leaf.received}/${leaf.total} received · pulling ${firstGap.start}–${firstGap.start + firstGap.count - 1}${moreNote}`;
    } else {
      label = 'Up to date';
      cmd = '';
      stateMsg = `complete — ${leaf.total} chunks`;
      disabled = true;
    }
  }

  function fire() {
    if (disabled || !cmd) return;
    onStage(cmd, args, txNode);
  }

  return (
    <div className="flex flex-col gap-1.5">
      <button
        disabled={disabled}
        onClick={fire}
        className="inline-flex items-center justify-center gap-2 rounded-sm border font-mono text-[12px] btn-feedback disabled:opacity-40"
        style={{
          height: 36,
          color: disabled ? colors.dim : tone,
          borderColor: disabled ? colors.borderSubtle : `${tone}66`,
          backgroundColor: disabled ? 'transparent' : `${tone}14`,
          fontWeight: 600,
          letterSpacing: '0.04em',
        }}
        title={cmd || 'nothing to do'}
      >
        {label.startsWith('Up to date') ? <Download className="size-3.5" /> : <RefreshCcw className="size-3.5" />}
        {label}
      </button>
      <span className="text-[11px] font-mono px-1" style={{ color: colors.dim }}>
        {stateMsg}
      </span>
    </div>
  );
}

// `cmdPrefix` removed — use `fileCaps(kind).{cntCmd,getCmd,deleteCmd}`
// from shared/fileKinds.ts (single source of truth, mirrored on backend).

// Range form — start/count only. The dest selector for IMG lives at the
// top of the deck (DestRow); duplicating it here was redundant.
//
// When `pendingRestage` is set (operator clicked a missing-range chip in
// the chunk timeline), the inputs remount via the chip's key and the
// start/count fields show the picked range.
function RangeBlock({
  txKind, txFilename, txNode, txDest, chunkSize, resolvedFile, resolvedLeaf,
  pendingRestage, onClearRestage, onStage,
}: {
  txKind: Kind;
  txFilename: string;
  txNode: Source;
  txDest: Dest;
  chunkSize: string;
  resolvedFile: DFile | null;
  resolvedLeaf: Leaf;
  pendingRestage: { start: number; count: number; key: number } | null;
  onClearRestage: () => void;
  onStage: (cmd_id: string, args?: Record<string, string>, dest?: string) => void;
}) {
  const leaf = resolvedFile ? leafTotals(resolvedFile, resolvedLeaf) : null;
  const state = leaf ? leafState(leaf) : 'discovered';
  const disabled = !resolvedFile || state === 'discovered';
  const startDefault = pendingRestage
    ? String(pendingRestage.start)
    : leaf ? String(leaf.received) : '';
  const countDefault = pendingRestage
    ? String(pendingRestage.count)
    : leaf && leaf.total > 0 ? String(Math.max(0, leaf.total - leaf.received)) : '';
  const inputKey = pendingRestage?.key ?? 'auto';
  const startRef = useRef<HTMLInputElement>(null);
  const countRef = useRef<HTMLInputElement>(null);

  function handleStage() {
    if (disabled || !txFilename) return;
    const startStr = startRef.current?.value ?? startDefault;
    const countStr = countRef.current?.value ?? countDefault;
    const start = Number(startStr);
    const count = Number(countStr);
    if (!Number.isFinite(start) || !Number.isFinite(count) || count <= 0) {
      showToast('Range invalid (count must be > 0)', 'warning');
      return;
    }
    const caps = fileCaps(txKind);
    const args: Record<string, string> = {
      filename: withExtension(txFilename, txKind),
      start_chunk: String(start),
      num_chunks: String(count),
      chunk_size: chunkSize,
    };
    if (caps.hasDestinationArg) args.destination = String(txDest);
    onStage(caps.getCmd, args, txNode);
    onClearRestage();
  }
  return (
    <div
      className="rounded-sm border px-2.5 py-2"
      style={{
        borderColor: colors.borderSubtle,
        backgroundColor: colors.bgApp,
        opacity: disabled ? 0.55 : 1,
      }}
    >
      <div className="flex items-center mb-1.5">
        <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: colors.dim }}>
          Stage range
        </span>
        {pendingRestage && (
          <span
            className="ml-2 inline-flex items-center gap-1 px-1.5 rounded-sm font-mono text-[11px] tabular-nums"
            style={{
              color: colors.info,
              backgroundColor: colors.infoFill,
              border: `1px solid ${colors.info}55`,
            }}
            title={`From missing-chunk picker: ${pendingRestage.start} … ${pendingRestage.start + pendingRestage.count - 1}`}
          >
            from missing
            <button
              onClick={onClearRestage}
              className="ml-1 inline-flex items-center justify-center"
              style={{ width: 12, height: 12, color: colors.info }}
              title="clear restage"
            >
              <X className="size-3" />
            </button>
          </span>
        )}
      </div>
      <div className="flex items-center gap-1.5 flex-wrap">
        <Mini key={`start-${inputKey}`} placeholder="start" w={60} defaultValue={startDefault} disabled={disabled} inputRef={startRef} />
        <Mini key={`count-${inputKey}`} placeholder="count" w={60} defaultValue={countDefault} disabled={disabled} inputRef={countRef} />
        <button
          disabled={disabled}
          onClick={handleStage}
          className="ml-auto px-3 rounded-sm font-mono text-[11px] btn-feedback disabled:opacity-50"
          style={{ height: 24, color: colors.bgBase, backgroundColor: colors.active, fontWeight: 600 }}
          title="Stage to queue"
        >
          stage
        </button>
      </div>
    </div>
  );
}

function SecondaryActions({
  txKind, txFilename, txNode, txDest, chunkSize, disabled, onStage, onDeleteRequest,
}: {
  txKind: Kind;
  txFilename: string;
  txNode: Source;
  txDest: Dest;
  chunkSize: string;
  disabled: boolean;
  onStage: (cmd_id: string, args?: Record<string, string>, dest?: string) => void;
  onDeleteRequest: () => void;
}) {
  const caps = fileCaps(txKind);
  const stagedName = txFilename ? withExtension(txFilename, txKind) : '';
  return (
    <div className="flex items-center gap-2">
      <SecondaryButton
        label="cnt"
        cmd={caps.cntCmd}
        target={txFilename}
        disabled={disabled}
        onClick={() => {
          const args: Record<string, string> = { filename: stagedName, chunk_size: chunkSize };
          if (caps.hasDestinationArg) args.destination = String(txDest);
          onStage(caps.cntCmd, args, txNode);
        }}
      />
      <SecondaryButton label="del" cmd={caps.deleteCmd} target={txFilename} disabled={disabled} destructive onClick={onDeleteRequest} />
    </div>
  );
}
function SecondaryButton({
  label, cmd, target, disabled, destructive, onClick,
}: { label: string; cmd: string; target: string; disabled?: boolean; destructive?: boolean; onClick?: () => void }) {
  const tone = destructive ? colors.danger : colors.dim;
  const fill = destructive ? colors.dangerFill : colors.neutralFill;
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className="flex-1 inline-flex items-center gap-2 px-2 rounded-sm border font-mono text-[11px] btn-feedback disabled:opacity-40"
      style={{
        height: 26,
        color: tone,
        borderColor: destructive ? `${colors.danger}55` : colors.borderSubtle,
        backgroundColor: fill,
      }}
      title={target ? `Stage ${cmd} for ${target}` : 'enter filename first'}
    >
      <span style={{ fontWeight: 600, letterSpacing: '0.04em', width: 26 }}>{label}</span>
      <span className="truncate" style={{ color: target ? colors.value : colors.sep }}>
        {target || '(no target)'}
      </span>
    </button>
  );
}

function ChunkSizeRow({
  value, onChange, lockedSize,
}: {
  value: string;
  onChange: (v: string) => void;
  /** Non-null when the resolved file's active leaf has been confirmed
   *  by a cnt RES (or when chunks have already flowed): input is forced
   *  to this value and disabled. */
  lockedSize: number | null;
}) {
  const locked = lockedSize != null;
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 shrink-0">
      <span className="text-[11px] uppercase tracking-wider font-semibold" style={{ color: colors.dim }}>
        chunk
      </span>
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        disabled={locked}
        className="font-mono px-2 rounded-sm border outline-none text-[11px] tabular-nums disabled:opacity-60"
        style={{
          width: 64, height: 22,
          backgroundColor: colors.bgApp,
          borderColor: locked ? `${colors.warning}55` : colors.borderSubtle,
          color: colors.textPrimary,
          cursor: locked ? 'not-allowed' : 'text',
        }}
        title={locked
          ? `Locked to ${lockedSize}B by the file's confirmed slicing — forget the local copy to recount at a different size`
          : 'bytes per chunk · applied to count + get'}
      />
      <span className="text-[11px] font-mono" style={{ color: colors.dim }}>B</span>
      {locked ? (
        <span
          className="inline-flex items-center gap-1 ml-auto font-mono text-[11px]"
          style={{ color: colors.warning }}
          title={`Locked to ${lockedSize}B`}
        >
          <Lock className="size-3" />locked
        </span>
      ) : (
        <span
          className="ml-auto font-mono text-[11px] italic"
          style={{ color: colors.dim }}
          title="No cnt response yet — operator can set any size; lock engages when the spacecraft acknowledges the count"
        >
          unlocked
        </span>
      )}
    </div>
  );
}

function StandaloneOps({
  kind, txNode, schema, onStage,
}: {
  kind: Kind;
  txNode: Source;
  schema: Record<string, Record<string, unknown>> | null;
  onStage: (cmd_id: string, args?: Record<string, string>, dest?: string) => void;
}) {
  const ops = STANDALONE_OPS[kind];
  if (ops.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: colors.dim }}>
        Standalone ({KIND_LABEL[kind]})
      </div>
      <div className="flex flex-col gap-1.5">
        {ops.map(o => (
          <StandaloneOpRow key={o.cmd} op={o} txNode={txNode} schema={schema} onStage={onStage} />
        ))}
      </div>
    </div>
  );
}

// Mirrors `isFilenameArg` in legacy FilesTxControls — name='filename' or
// type='Filename' both flag a filename-shaped argument so the row uses
// FilenameInput and applies withExtension on stage.
function isFilenameArg(arg: TxArgSchema): boolean {
  return arg.name === 'filename' || arg.type === 'Filename';
}

function StandaloneOpRow({
  op, txNode, schema, onStage,
}: {
  op: StandaloneOpDef;
  txNode: Source;
  schema: Record<string, Record<string, unknown>> | null;
  onStage: (cmd_id: string, args?: Record<string, string>, dest?: string) => void;
}) {
  const txArgs = ((schema?.[op.cmd] as { tx_args?: TxArgSchema[] } | undefined)?.tx_args) ?? [];
  const [values, setValues] = useState<Record<string, string>>(op.initialValues ?? {});
  // Required-arg gate: any non-optional arg with empty value blocks
  // stage — same contract the backend would enforce on submit.
  const missingRequired = txArgs.some(
    arg => !arg.optional && (values[arg.name] ?? '').trim() === '',
  );
  // Stage stays disabled until /api/schema returns. Without the schema
  // we don't know which args are required, so submitting initialValues
  // verbatim could push backend-rejected args (e.g. mag_capture is
  // missing filename in initialValues by design — operator MUST type
  // one). The fetch happens once on mount so the disabled window is
  // brief.
  const schemaMissing = schema == null;
  const stageDisabled = schemaMissing || missingRequired;

  // Filename-arg kind — mostly the surrounding op's kind, but
  // overridable per-arg (e.g. aii_img.filename names a JPEG, not an AII
  // record — same convention as fileKinds.ts::extraCmdFilenameKind).
  function filenameKindFor(argName: string): Kind {
    return op.filenameKindOverride?.[argName] ?? op.filenameDefaultKind ?? 'image';
  }

  function handleStage() {
    if (stageDisabled) return;
    const submitArgs: Record<string, string> = {};
    for (const arg of txArgs) {
      const raw = (values[arg.name] ?? '').trim();
      if (raw === '' && arg.optional) continue;
      submitArgs[arg.name] = isFilenameArg(arg)
        ? withExtension(raw, filenameKindFor(arg.name))
        : raw;
    }
    onStage(op.cmd, submitArgs, txNode);
  }

  const showInputs = txArgs.length > 0 && schema != null;

  return (
    <div
      className="rounded-sm border px-2 py-1.5"
      style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgApp }}
    >
      <div className="flex items-center gap-2">
        <span
          className="inline-flex items-center justify-center font-mono text-[11px] rounded-sm shrink-0"
          style={{
            width: 36, height: 18,
            color: op.destructive ? colors.danger : colors.dim,
            backgroundColor: op.destructive ? `${colors.danger}10` : `${colors.dim}14`,
            border: `1px solid ${op.destructive ? colors.danger + '44' : colors.borderSubtle}`,
            letterSpacing: '0.04em',
            fontWeight: 600,
          }}
        >
          {op.label}
        </span>
        <div className="flex flex-col min-w-0 flex-1">
          <span className="font-mono text-[11px] truncate" style={{ color: colors.textPrimary }} title={op.cmd}>
            {op.cmd}
          </span>
          {op.note && (
            <span className="font-mono text-[11px] truncate" style={{ color: colors.sep }}>
              {op.note}
            </span>
          )}
        </div>
        {!showInputs && (
          <button
            disabled={stageDisabled}
            onClick={handleStage}
            className="px-2 rounded-sm font-mono text-[11px] btn-feedback shrink-0 disabled:opacity-40"
            style={{ height: 22, color: colors.bgBase, backgroundColor: colors.active, fontWeight: 600 }}
            title={schemaMissing ? `Stage ${op.cmd} (schema loading…)` : `Stage ${op.cmd}`}
          >
            stage
          </button>
        )}
      </div>
      {showInputs && (
        <div className="flex items-end gap-1.5 flex-wrap mt-1.5">
          {txArgs.map(arg => {
            const isFile = isFilenameArg(arg);
            const v = values[arg.name] ?? '';
            return (
              <div key={arg.name} style={isFile ? { flex: '1 1 140px', minWidth: 120 } : undefined}>
                <div
                  className="text-[11px] uppercase tracking-wider mb-0.5 font-semibold"
                  style={{ color: colors.dim }}
                  title={arg.description ?? arg.type}
                >
                  {arg.name}
                  {arg.optional && <span className="ml-0.5" style={{ color: colors.sep }}>?</span>}
                </div>
                {isFile ? (
                  <FilenameInput
                    kind={filenameKindFor(arg.name)}
                    value={v}
                    onChange={(nv) => setValues(prev => ({ ...prev, [arg.name]: nv }))}
                  />
                ) : (
                  <input
                    value={v}
                    onChange={e => setValues(prev => ({ ...prev, [arg.name]: e.target.value }))}
                    title={arg.description ?? arg.type}
                    className="px-1.5 rounded-sm border outline-none font-mono text-[11px]"
                    style={{
                      width: 78, height: 22,
                      backgroundColor: colors.bgPanel,
                      borderColor: colors.borderSubtle,
                      color: colors.textPrimary,
                    }}
                  />
                )}
              </div>
            );
          })}
          <div className="flex-1" />
          <button
            disabled={stageDisabled}
            onClick={handleStage}
            className="px-2 rounded-sm font-mono text-[11px] btn-feedback shrink-0 disabled:opacity-40"
            style={{ height: 22, color: colors.bgBase, backgroundColor: colors.active, fontWeight: 600 }}
            title={stageDisabled ? `${op.cmd} needs all required args` : `Stage ${op.cmd}`}
          >
            stage
          </button>
        </div>
      )}
    </div>
  );
}

// StandaloneOps inputs are now driven by `/api/schema`. The metadata
// here is intentionally minimal: which cmds to surface, the row label/
// note/destructive flag, sensible initial values for inputs, and an
// optional per-arg filename-kind override (aii_img.filename names a
// JPEG, not an AII file). Adding a new arg in mission.yml will appear
// automatically without editing this file.
interface StandaloneOpDef {
  label: string;
  cmd: string;
  destructive?: boolean;
  note?: string;
  /** Initial input values, applied to the matching schema arg by name.
   *  Operator can edit before staging. */
  initialValues?: Record<string, string>;
  /** Default file-kind for any filename-shaped arg. Falls back to
   *  'image' when omitted. */
  filenameDefaultKind?: Kind;
  /** Per-arg override of the filename-kind. */
  filenameKindOverride?: Record<string, Kind>;
}
const STANDALONE_OPS: Record<Kind, ReadonlyArray<StandaloneOpDef>> = {
  image: [],
  aii: [
    { label: 'dir', cmd: 'aii_dir', note: 'discover ranking', initialValues: { destination: '1', max_tx: '10' } },
    {
      label: 'img', cmd: 'aii_img', note: 'AII record by image',
      initialValues: { destination: '1' },
      // aii_img.filename names the JPEG, per
      // shared/fileKinds.ts::extraCmdFilenameKind.
      filenameKindOverride: { filename: 'image' },
    },
  ],
  mag: [
    { label: 'cap',  cmd: 'mag_capture', note: 'out-of-pass', initialValues: { time: '5', mode: '1' }, filenameDefaultKind: 'mag' },
    { label: 'kill', cmd: 'mag_kill', destructive: true },
    { label: 'tlm',  cmd: 'mag_tlm' },
  ],
};

function StagedSection({
  rows, onAbort, onClear, onSendAll,
}: {
  rows: ReadonlyArray<StagedRow>;
  /** Aborts the currently-sending row. Only rendered on the in-flight
   *  row; mirrors the global `abortSend` semantics — no per-row queue
   *  index is needed. */
  onAbort: () => void;
  onClear: () => void;
  onSendAll: () => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      {/* Header lives on the wrapping `StagedPanel` (title + count
          sub). No duplicate label here. */}
      <div className="flex flex-col gap-1">
        {rows.map(s => {
          const c = STAGE_COLOR[s.stage];
          const inFlight = s.stage === 'received' || s.stage === 'accepted';
          return (
            <div
              key={s.num}
              className="rounded-sm border px-2 py-1.5"
              style={{
                borderColor: inFlight ? `${c}55` : colors.borderSubtle,
                backgroundColor: inFlight ? `${c}0F` : colors.bgApp,
              }}
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-[11px] tabular-nums shrink-0" style={{ color: colors.sep, width: 14 }}>{s.num}</span>
                <span className="font-mono text-[11px] truncate flex-1 min-w-0" style={{ color: colors.textPrimary }}>{s.cmdId}</span>
                <StageBadge stage={s.stage} />
                {/* Abort affordance — visible on in-flight items only.
                    Mirrors the existing `abortSend` flow. */}
                {inFlight && (
                  <button
                    onClick={() => onAbort()}
                    className="inline-flex items-center justify-center shrink-0"
                    style={{ width: 16, height: 16, color: colors.danger }}
                    title={`Abort ${s.cmdId}`}
                  >
                    <X className="size-3" />
                  </button>
                )}
              </div>
              <div
                className="text-[11px] font-mono truncate mt-0.5"
                style={{ color: colors.dim, paddingLeft: 22 }}
              >
                {s.sub || '(no params)'}
              </div>
              {inFlight && s.pct > 0 && (
                <div className="mt-1.5 ml-[22px]">
                  <ProgressBar pct={s.pct} complete={false} fresh />
                </div>
              )}
            </div>
          );
        })}
        {rows.length === 0 && (
          <div className="text-[11px] font-mono text-center py-3" style={{ color: colors.dim }}>
            nothing staged
          </div>
        )}
      </div>
      {rows.length > 0 && (
        <div className="flex items-center gap-2">
          <button
            onClick={onClear}
            className="inline-flex items-center gap-1 px-2 rounded-sm font-mono text-[11px] btn-feedback"
            style={{ height: 22, color: colors.dim }}
          >
            <Trash2 className="size-3" />Clear
          </button>
          <div className="flex-1" />
          <button
            onClick={onSendAll}
            className="inline-flex items-center gap-1.5 px-3 rounded-sm font-mono text-[11px] btn-feedback"
            style={{ height: 24, color: colors.bgBase, backgroundColor: colors.success, fontWeight: 600 }}
          >
            <Send className="size-3" />Send all
          </button>
        </div>
      )}
    </div>
  );
}

function StageBadge({ stage }: { stage: VerifierStage }) {
  const c = STAGE_COLOR[stage];
  return (
    <span
      className="ml-auto inline-flex items-center font-mono text-[11px] font-bold tracking-wider shrink-0"
      style={{
        color: c, height: 14, padding: '0 4px', borderRadius: 2,
        border: `1px solid ${c}55`, backgroundColor: `${c}14`,
        letterSpacing: '0.05em',
      }}
      title={`verifier stage: ${stage}`}
    >
      {STAGE_LABEL[stage]}
    </span>
  );
}

function Mini({
  placeholder, w = 64, defaultValue, disabled, inputRef,
}: {
  placeholder: string;
  w?: number;
  defaultValue?: string;
  disabled?: boolean;
  inputRef?: React.Ref<HTMLInputElement>;
}) {
  return (
    <input
      ref={inputRef}
      disabled={disabled}
      className="font-mono px-1.5 rounded-sm border outline-none text-[11px] tabular-nums disabled:opacity-50"
      style={{ width: w, height: 22, backgroundColor: colors.bgApp, borderColor: colors.borderSubtle, color: colors.textPrimary }}
      placeholder={placeholder}
      defaultValue={defaultValue}
    />
  );
}

// ─── ACTIVITY TAIL ───────────────────────────────────────────────────

function ActivityTail({
  rows, expanded, onToggle,
}: { rows: ReadonlyArray<ActivityRow>; expanded: boolean; onToggle: () => void }) {
  // "Receiving" is a windowed signal — true only while a new file-event
  // packet arrived in the last RECEIVING_WINDOW_MS. Without the window
  // the flag would latch ON forever after the first RX (every row in
  // `rows` is dir='RX' by construction), so the green sweep + pulse
  // would never clear. Resets via setTimeout when no new row arrives
  // within the window. Dep is `last?.tsRel`, NOT `rows.length` —
  // `liveActivity` is `slice(-50)` so once 50 packets have landed,
  // `length` saturates and won't fire the effect on subsequent
  // arrivals. The latest packet's relative timestamp does change.
  const last = rows[rows.length - 1];
  const lastTs = last?.tsRel ?? -1;
  const [receiving, setReceiving] = useState(false);
  const RECEIVING_WINDOW_MS = 1500;
  useEffect(() => {
    if (rows.length === 0) {
      setReceiving(false);
      return;
    }
    setReceiving(true);
    const t = setTimeout(() => setReceiving(false), RECEIVING_WINDOW_MS);
    return () => clearTimeout(t);
  }, [lastTs, rows.length]);
  return (
    <PanelShell
      style={{
        height: expanded ? 220 : 34,
        transition: 'height 200ms ease, border-color 160ms ease',
        borderColor: receiving ? `${colors.success}55` : colors.borderSubtle,
      }}
    >
      <button
        onClick={onToggle}
        className={`flex items-center gap-2 px-3 shrink-0 border-b ${receiving ? 'animate-sweep-green' : ''}`}
        style={{
          borderColor: colors.borderSubtle,
          minHeight: 34, paddingTop: 6, paddingBottom: 6,
          backgroundColor: receiving ? `${colors.success}08` : 'transparent',
          transition: 'background-color 160ms ease',
        }}
      >
        <Download className="size-3.5 shrink-0" style={{ color: colors.dim }} />
        <span
          className="font-bold uppercase shrink-0"
          style={{ color: colors.value, fontSize: 14, letterSpacing: '0.02em' }}
        >
          Events
        </span>
        {receiving ? (
          <span className="text-[11px] font-bold animate-pulse-text flex items-center gap-1" style={{ color: colors.success }}>
            <Download className="size-3" />
            Received
          </span>
        ) : (
          <span className="text-[11px]" style={{ color: colors.textMuted }}>Idle</span>
        )}
        {!expanded && last && <ActivityTickerInline last={last} />}
        <div className="flex-1" />
        <span className="font-mono text-[11px] tabular-nums" style={{ color: colors.dim }}>
          {rows.length} pkt{rows.length === 1 ? '' : 's'}
        </span>
        {expanded ? <ChevronDown className="size-3.5" style={{ color: colors.dim }} /> : <ChevronUp className="size-3.5" style={{ color: colors.dim }} />}
      </button>
      {expanded && (
        <div className="flex-1 overflow-auto min-h-0">
          {/* Packet-row table — matches the existing RxLogPanel idiom.
              Columns: time · DIR · PTYPE badge · src · cmd_id · meta. */}
          <div
            className="grid items-center px-3 py-1 sticky top-0"
            style={{
              gridTemplateColumns: '50px 28px 56px 50px 1fr 1fr',
              borderBottom: `1px solid ${colors.borderSubtle}`,
              backgroundColor: colors.bgPanel,
              gap: 8,
            }}
          >
            <ActivityCol>time</ActivityCol>
            <ActivityCol>dir</ActivityCol>
            <ActivityCol>ptype</ActivityCol>
            <ActivityCol>src</ActivityCol>
            <ActivityCol>cmd_id</ActivityCol>
            <ActivityCol>meta</ActivityCol>
          </div>
          {rows.length === 0 && (
            <div className="px-3 py-3 text-center italic text-[11px]" style={{ color: colors.textMuted }}>
              no file events yet
            </div>
          )}
          {rows.map((row, i) => {
            const ptyTone = PTYPE_TONE[row.ptype];
            const dirTone = row.dir === 'TX' ? colors.info : colors.success;
            return (
              <div
                key={i}
                className="grid items-center px-3 py-1 text-[11px] font-mono"
                style={{
                  gridTemplateColumns: '50px 28px 56px 50px 1fr 1fr',
                  color: colors.textPrimary,
                  borderBottom: `1px solid ${colors.borderSubtle}33`,
                  gap: 8,
                }}
                title={row.meta}
              >
                <span style={{ color: colors.sep }} className="tabular-nums">+{row.tsRel.toFixed(1)}s</span>
                <span style={{ color: dirTone, fontWeight: 600 }}>{row.dir}</span>
                <span
                  className="text-center tabular-nums"
                  style={{
                    color: ptyTone,
                    backgroundColor: `${ptyTone}10`,
                    border: `1px solid ${ptyTone}33`,
                    borderRadius: 2,
                    padding: '0 4px',
                    fontWeight: 600,
                    letterSpacing: '0.04em',
                  }}
                >
                  {row.ptype}
                </span>
                <span style={{ color: colors.dim }}>{row.src}</span>
                <span style={{ color: colors.value }} className="truncate">{row.cmd}</span>
                <span style={{ color: colors.dim }} className="truncate">{row.meta}</span>
              </div>
            );
          })}
        </div>
      )}
    </PanelShell>
  );
}

function ActivityTickerInline({ last }: { last: ActivityRow }) {
  const tone = last.dir === 'TX' ? colors.info : colors.success;
  const ptyTone = PTYPE_TONE[last.ptype];
  return (
    <div className="flex items-center gap-2 ml-2 truncate">
      <span style={{ color: tone, fontWeight: 600 }} className="font-mono text-[11px] shrink-0">{last.dir}</span>
      <span
        className="font-mono text-[11px] shrink-0 tabular-nums"
        style={{
          color: ptyTone,
          backgroundColor: `${ptyTone}10`,
          border: `1px solid ${ptyTone}33`,
          borderRadius: 2,
          padding: '0 4px',
          fontWeight: 600,
        }}
      >
        {last.ptype}
      </span>
      <span className="font-mono text-[11px] truncate" style={{ color: colors.value }}>{last.cmd}</span>
      <span className="font-mono text-[11px] truncate" style={{ color: colors.dim }}>· {last.meta}</span>
    </div>
  );
}

function ActivityCol({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[11px] uppercase tracking-wider font-semibold" style={{ color: colors.dim }}>
      {children}
    </span>
  );
}

// ─── CAPTURE SHEET ───────────────────────────────────────────────────

function CaptureSheet({
  txNode, onClose, onStage,
}: {
  txNode: Source;
  onClose: () => void;
  onStage: (cmd_id: string, args?: Record<string, string>, dest?: string) => void;
}) {
  // Refs for the form fields. Arg names match the real `cam_capture`
  // schema observed in production logs (quantity/dt/exposure_us/k_cap/
  // k_thumb/quality), not the friendly form labels. Filename gets the
  // .jpg extension auto-appended via withExtension.
  const filenameRef = useRef<HTMLInputElement>(null);
  const quantityRef = useRef<HTMLInputElement>(null);
  const dtRef       = useRef<HTMLInputElement>(null);
  const focusRef    = useRef<HTMLInputElement>(null);
  const exposureRef = useRef<HTMLInputElement>(null);
  const kCapRef     = useRef<HTMLInputElement>(null);
  const kThumbRef   = useRef<HTMLInputElement>(null);
  const qualityRef  = useRef<HTMLInputElement>(null);

  function handleStage() {
    const filename = filenameRef.current?.value.trim();
    if (!filename) { showToast('Capture needs a filename', 'warning'); return; }
    onStage('cam_capture', {
      filename:    withExtension(filename, 'image'),
      quantity:    quantityRef.current?.value || '1',
      dt:          dtRef.current?.value       || '0',
      focus:       focusRef.current?.value    || 'auto',
      exposure_us: exposureRef.current?.value || '20000',
      k_cap:       kCapRef.current?.value     || '1.0',
      k_thumb:     kThumbRef.current?.value   || '0.25',
      quality:     qualityRef.current?.value  || '80',
    }, txNode);
    onClose();
  }

  return (
    <div
      className="absolute inset-0 flex items-center justify-center"
      style={{ backgroundColor: colors.modalBackdrop, backdropFilter: 'blur(2px)', zIndex: 30 }}
      onClick={onClose}
    >
      <div
        className="w-[640px] rounded-md border shadow-panel"
        style={{ borderColor: `${colors.success}55`, backgroundColor: colors.bgPanel, boxShadow: `0 0 60px ${colors.success}20` }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="flex items-center gap-2 px-4 border-b"
          style={{ borderColor: colors.borderSubtle, height: 38 }}
        >
          <Camera className="size-4" style={{ color: colors.success }} />
          <span className="font-bold uppercase font-mono text-[13px]" style={{ color: colors.value, letterSpacing: '0.04em' }}>
            Capture
          </span>
          <span className="text-[11px] font-mono" style={{ color: colors.sep }}>cam_capture · out-of-pass operation</span>
          <div className="flex-1" />
          <button
            onClick={onClose}
            className="inline-flex items-center justify-center"
            style={{ width: 22, height: 22, color: colors.dim }}
          >
            <X className="size-4" />
          </button>
        </div>
        <div className="grid grid-cols-3 gap-3 p-4">
          <CapField label="Filename"     ph="capture_009" inputRef={filenameRef} />
          <CapField label="Quantity"     ph="1"           inputRef={quantityRef} defaultValue="1" />
          <CapField label="dt (s)"       ph="0"           inputRef={dtRef}       defaultValue="0" />
          <CapField label="Focus"        ph="auto"        inputRef={focusRef}    defaultValue="auto" />
          <CapField label="Exposure µs"  ph="20000"       inputRef={exposureRef} defaultValue="20000" />
          <CapField label="K cap"        ph="1.0"         inputRef={kCapRef}     defaultValue="1.0" />
          <CapField label="K thumb"      ph="0.25"        inputRef={kThumbRef}   defaultValue="0.25" />
          <CapField label="Quality"      ph="80"          inputRef={qualityRef}  defaultValue="80" />
        </div>
        <div className="flex items-center gap-2 px-4 py-3 border-t" style={{ borderColor: colors.borderSubtle }}>
          <span className="text-[11px] font-mono" style={{ color: colors.dim }}>
            {txNode} · cam_capture
          </span>
          <div className="flex-1" />
          <button
            className="px-3 rounded-sm font-mono text-[11px]"
            style={{ height: 26, color: colors.dim }}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            onClick={handleStage}
            className="inline-flex items-center gap-1.5 px-3 rounded-sm font-mono text-[11px] btn-feedback"
            style={{ height: 26, color: colors.bgBase, backgroundColor: colors.active, fontWeight: 600 }}
            title="Stage cam_capture into the queue"
          >
            <Send className="size-3" />Stage capture
          </button>
        </div>
      </div>
    </div>
  );
}

function CapField({
  label, ph, inputRef, defaultValue,
}: {
  label: string;
  ph: string;
  inputRef?: React.Ref<HTMLInputElement>;
  defaultValue?: string;
}) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider mb-1 font-semibold" style={{ color: colors.dim }}>{label}</div>
      <input
        ref={inputRef}
        defaultValue={defaultValue}
        className="w-full px-2 rounded-sm border outline-none font-mono text-[11px]"
        style={{ height: 24, backgroundColor: colors.bgApp, borderColor: colors.borderSubtle, color: colors.textPrimary }}
        placeholder={ph}
      />
    </div>
  );
}

// Custom `ConfirmDeleteSheet` removed in the architecture-cleanliness
// pass — replaced by `@/components/shared/dialogs/ConfirmDialog` which is
// what `FilesPage` already uses for its destructive flows.

// ─── LCD DISPLAY SHEET ───────────────────────────────────────────────
// `lcd_display` shows an image already stored on-board on the LCD panel.
// Most-used LCD command in real sessions (6× TX in the GS-1 sample log).

function LcdDisplaySheet({
  txNode, onClose, onStage,
}: {
  txNode: Source;
  onClose: () => void;
  onStage: (cmd_id: string, args?: Record<string, string>, dest?: string) => void;
}) {
  // Real lcd_display schema: filename + destination. `lcd_dest_t` valid
  // range is [0, 1] (0 = presaved, 1 = captured) per mission.yml — the
  // shared DEST_OPTIONS exposes a thumbnails(2) entry for img_dest_t,
  // which is not valid here, so filter it out.
  const filenameRef = useRef<HTMLInputElement>(null);
  const [dest, setDest] = useState<Dest>(1);
  const lcdDestOptions = DEST_OPTIONS.filter(opt => opt.id !== 2);

  function handleStage() {
    const filename = filenameRef.current?.value.trim();
    if (!filename) { showToast('lcd_display needs a filename', 'warning'); return; }
    onStage('lcd_display', {
      filename:    withExtension(filename, 'image'),
      destination: String(dest),
    }, txNode);
    onClose();
  }

  return (
    <div
      className="absolute inset-0 flex items-center justify-center"
      style={{ backgroundColor: colors.modalBackdrop, backdropFilter: 'blur(2px)', zIndex: 30 }}
      onClick={onClose}
    >
      <div
        className="w-[520px] rounded-md border shadow-panel"
        style={{ borderColor: `${colors.active}55`, backgroundColor: colors.bgPanel, boxShadow: `0 0 60px ${colors.active}20` }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="flex items-center gap-2 px-4 border-b"
          style={{ borderColor: colors.borderSubtle, height: 38 }}
        >
          <Monitor className="size-4" style={{ color: colors.active }} />
          <span className="font-bold uppercase font-mono text-[13px]" style={{ color: colors.value, letterSpacing: '0.04em' }}>
            LCD Display
          </span>
          <span className="text-[11px] font-mono" style={{ color: colors.sep }}>lcd_display</span>
          <div className="flex-1" />
          <button
            onClick={onClose}
            className="inline-flex items-center justify-center"
            style={{ width: 22, height: 22, color: colors.dim }}
          >
            <X className="size-4" />
          </button>
        </div>
        <div className="p-4 space-y-3">
          <CapField label="Filename" ph="out (extension auto)" inputRef={filenameRef} />
          <div>
            <div className="text-[11px] uppercase tracking-wider mb-1 font-semibold" style={{ color: colors.dim }}>Destination</div>
            <div className="flex items-center gap-px rounded-sm overflow-hidden" style={{ border: `1px solid ${colors.borderSubtle}`, width: 'fit-content' }}>
              {lcdDestOptions.map(opt => {
                const active = dest === opt.id;
                return (
                  <button
                    key={opt.id}
                    onClick={() => setDest(opt.id)}
                    title={opt.title}
                    className="px-3 font-mono text-[11px] btn-feedback"
                    style={{
                      height: 24,
                      color: active ? colors.bgBase : colors.dim,
                      backgroundColor: active ? colors.active : 'transparent',
                      fontWeight: active ? 600 : 400,
                      letterSpacing: '0.06em',
                    }}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 px-4 py-3 border-t" style={{ borderColor: colors.borderSubtle }}>
          <span className="text-[11px] font-mono" style={{ color: colors.dim }}>
            {txNode} · lcd_display
          </span>
          <div className="flex-1" />
          <button
            className="px-3 rounded-sm font-mono text-[11px]"
            style={{ height: 26, color: colors.dim }}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            onClick={handleStage}
            className="inline-flex items-center gap-1.5 px-3 rounded-sm font-mono text-[11px] btn-feedback"
            style={{ height: 26, color: colors.bgBase, backgroundColor: colors.active, fontWeight: 600 }}
          >
            <Send className="size-3" />Stage display
          </button>
        </div>
      </div>
    </div>
  );
}

