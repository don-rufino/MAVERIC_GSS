/**
 * Downlink — read-only file browser.
 *
 *   ┌──────────────────────────────────────────────────────────────────┐
 *   │ topbar · DOWNLINK · filter · read-only · command via TX pane       │
 *   ├──────┬─────────────────────────────────────────────────────────────┤
 *   │ pick │ PREVIEW                                                      │
 *   │ 230  │  header · kind · [FULL][THUMB] · name · src · download       │
 *   │ live │  ┌───────────────────────────┬──────────────┐               │
 *   │ idle │  │ progressive image preview │ image meta    │               │
 *   │ done │  └───────────────────────────┴──────────────┘               │
 *   │      │  chunk map — per-chunk green/red dots + missing ranges       │
 *   ├──────┴─────────────────────────────────────────────────────────────┤
 *   │ events (collapsible) — file-only RX packet log                       │
 *   └──────────────────────────────────────────────────────────────────┘
 *
 * Commanding (count / get / delete-on-spacecraft / cam / lcd / mag) is NOT
 * issued here — operators run those through the normal TX pane. This page
 * is browse + inspect only:
 *   - useImageFiles + useFlatFiles('aii'/'mag')  — file lists
 *   - usePluginServices().packets                 — file-only activity log
 *   - useFileChunkSet                             — per-chunk received set
 *   - files/JsonPreview, files/MagPreview         — flat-file content panes
 *
 * The one local affordance that remains is the picker row trash: a
 * ground-station "forget local copy" (HTTP DELETE of accumulated chunks).
 * It never touches the on-board copy, so it is not spacecraft commanding.
 */
import { useEffect, useMemo, useState } from 'react';
import {
  ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Download, FileBox,
  FileJson, Image as ImageIcon, Search, Trash2, X, type LucideIcon,
} from 'lucide-react';
import { colors } from '@/lib/colors';
import { usePluginServices } from '@/hooks/usePluginServices';
import { ConfirmDialog } from '@/components/shared/dialogs/ConfirmDialog';
import { showToast } from '@/components/shared/overlays/StatusToast';
import { useImageFiles, useFlatFiles, useFileChunks } from '../files/FileChunkContext';
import { JsonPreview } from '../files/JsonPreview';
import { MagPreview } from '../files/MagPreview';
import { filesEndpoint } from '../files/helpers';
import { useFileChunkSet, type ChunkSetTarget } from '../shared/useFileChunkSet';
import { computeMissingRanges } from '../shared/missingRanges';
import { packetPayloadText } from '@/lib/rxPacket';
import { mavericCmdId, mavericHeader, mavericPtype } from '@/plugins/maveric/missionFacts';
import type { FileLeaf, ImagePair } from '../files/types';

// ─── MODEL ───────────────────────────────────────────────────────────

type Kind = 'image' | 'aii' | 'mag';
type Source = 'HLNV' | 'ASTR';
type FileState = 'discovered' | 'counted' | 'in-flight' | 'complete';
type Leaf = 'full' | 'thumb';

interface LeafData { received: number; total: number; chunkSize: number }
interface ImageFile { id: string; kind: 'image'; source: Source; stem: string; full: LeafData; thumb: LeafData | null; ageS: number; }
interface FlatFile  { id: string; kind: 'aii' | 'mag'; source: Source; filename: string; received: number; total: number; chunkSize: number; ageS: number; }
type DFile = ImageFile | FlatFile;

type PType = 'CMD' | 'CHUNK' | 'RES' | 'ACK' | 'TLM' | 'REQ';
interface ActivityRow {
  tsRel: number;     // seconds-from-start (display only)
  dir: 'TX' | 'RX';
  ptype: PType;
  src: string;       // GS / HLNV / ASTR / UPPM / LPPM / EPS
  cmd: string;
  meta: string;
}

// Predicate: command originates from the Files page (imaging / AII / MAG
// transfers plus the cam/lcd controls). Drives the Activity log filter so
// only file-relevant traffic shows here. Excludes TLM beacons, EPS HK,
// MTQ/GNC/PPM ops which belong to other pages.
const FILES_PAGE_CMD_RE = /^(img|aii|mag|cam|lcd)_/;
function isFilesPageCmd(cmd: string): boolean {
  return FILES_PAGE_CMD_RE.test(cmd);
}

const PTYPE_TONE: Record<PType, string> = {
  CHUNK: colors.success,  // RX data
  RES:   colors.success,  // RES per badgeToneMap
  ACK:   colors.info,     // ACK per badgeToneMap
  TLM:   colors.active,   // TLM per badgeToneMap
  CMD:   colors.neutral,  // CMD per badgeToneMap
  REQ:   colors.neutral,  // REQ per badgeToneMap
};

const STATE_TONE: Record<FileState, string> = {
  discovered: colors.neutral,
  counted:    colors.info,
  'in-flight': colors.info,
  complete:   colors.success,
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
    // A placeholder thumb (received=0, total=0) shouldn't drive the local
    // leaf data — fall back to full so progress + state read coherently.
    if (leaf === 'thumb' && f.thumb && (f.thumb.received > 0 || f.thumb.total > 0)) {
      return f.thumb;
    }
    return f.full;
  }
  return { received: f.received, total: f.total, chunkSize: f.chunkSize };
}
function fileName(f: DFile): string { return f.kind === 'image' ? f.stem : f.filename; }
function leafFilename(f: DFile, leaf: Leaf): string {
  if (f.kind === 'image' && leaf === 'thumb') return `tn_${f.stem}`;
  return fileName(f);
}
// Distinguish a real (counted or partially-received) thumb from the
// backend's placeholder leaf. The status adapter always returns a thumb
// entry so the JSON shape is uniform; a real thumb carries total > 0
// (after the cnt RES) or received > 0 (chunks arrived ahead of the count).
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
// model. Live `total` / `chunk_size` are nullable (null = "discovered,
// not counted"); the state machine treats `total === 0` as discovered, so
// null → 0 keeps the existing behaviour. Source defaults to 'HLNV'.

function asSource(s: string | null | undefined): Source {
  return s === 'ASTR' ? 'ASTR' : 'HLNV';
}
function liveLeaf(l: FileLeaf): LeafData {
  return { received: l.received, total: l.total ?? 0, chunkSize: l.chunk_size ?? 125 };
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
    chunkSize: f.chunk_size ?? 125,
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

export default function DownlinkPreview() {
  // Live data hooks. usePluginServices is mounted at App level; the
  // FileChunkProvider wraps all maveric plugin pages (see providers.ts)
  // so useImageFiles / useFlatFiles work here without extra setup. Only
  // `packets` is consumed — the command surface lives in the TX pane.
  const { packets } = usePluginServices();
  const imageFiles = useImageFiles();
  const aiiFiles   = useFlatFiles('aii');
  const magFiles   = useFlatFiles('mag');
  const { setLastTouchedFlatKind } = useFileChunks();

  // Compose into the preview's DFile shape. Recomputed on every render —
  // packets/files arrays are reference-stable from the providers, and the
  // adapter is cheap.
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

  // File-only RX activity. TX events live in the TX pane, not here. Capped
  // at 50 most recent.
  const liveActivity: ActivityRow[] = useMemo(() => {
    const out: ActivityRow[] = [];
    const startMs = packets[0]?.received_at_ms ?? 0;
    for (const p of packets) {
      if (p.is_echo) continue;
      const cmdId = mavericCmdId(p);
      if (!cmdId || !isFilesPageCmd(cmdId)) continue;
      const header = mavericHeader(p);
      const rawPty = mavericPtype(p) || 'CMD';
      const ptype: PType = (rawPty in PTYPE_TONE) ? (rawPty as PType) : 'CMD';
      const args = packetPayloadText(p, { compact: true });
      out.push({
        tsRel: p.received_at_ms != null ? Math.max(0, (p.received_at_ms - startMs) / 1000) : 0,
        dir: 'RX',
        ptype,
        src: String(header?.src ?? '?'),
        cmd: cmdId,
        meta: args || `pkt #${p.num}`,
      });
    }
    return out.slice(-50);
  }, [packets]);

  // Seed focus from any existing per-kind selection so opening this tab
  // inherits whatever was last touched elsewhere.
  const [focusedId, setFocusedId] = useState<string>(() => (
    imageFiles.selectedId || aiiFiles.selectedId || magFiles.selectedId || ''
  ));
  useEffect(() => {
    if (focusedId) return;
    const candidate = imageFiles.selectedId || aiiFiles.selectedId || magFiles.selectedId;
    if (candidate) setFocusedId(candidate);
  }, [focusedId, imageFiles.selectedId, aiiFiles.selectedId, magFiles.selectedId]);

  const [activeLeaf, setActiveLeaf] = useState<Leaf>('full');
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<FilterKind>('all');
  const [activityExpanded, setActivityExpanded] = useState(false);
  const [pickerCollapsed, setPickerCollapsed] = useState(false);
  // Ground-station "forget local copy" — HTTP DELETE of accumulated chunks.
  // Never touches the on-board copy, so this is not spacecraft commanding.
  const [pendingDelete, setPendingDelete] = useState<{ file: DFile } | null>(null);

  const focused = liveFiles.find(f => f.id === focusedId) ?? null;

  // Original FileLeaf for the focused AII/MAG file — handed to the
  // existing JsonPreview / MagPreview components.
  const focusedLeaf: FileLeaf | null = useMemo(() => {
    if (!focused || focused.kind === 'image') return null;
    const list = focused.kind === 'aii' ? aiiFiles.files : magFiles.files;
    return list.find(f => f.id === focused.id) ?? null;
  }, [focused, aiiFiles.files, magFiles.files]);

  // Image focus needs the original FileLeaf (full or thumb) so the preview
  // <img> can hit the preview endpoint and the chunk map can fetch real
  // received-chunk indices. Falls back to the full leaf when the active
  // leaf is thumb but no real thumb exists yet.
  const focusedImageLeaf: FileLeaf | null = useMemo(() => {
    if (!focused || focused.kind !== 'image') return null;
    const pair = imageFiles.files.find(p => p.id === focused.id);
    if (!pair) return null;
    const wantThumb = activeLeaf === 'thumb' && pair.thumb && (
      pair.thumb.received > 0 || (pair.thumb.total ?? 0) > 0
    );
    return wantThumb ? pair.thumb : pair.full;
  }, [focused, activeLeaf, imageFiles.files]);

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

  // Focusing a non-image (or an image without thumb) forces the leaf back
  // to full so the toggle never lands on a non-existent thumb. Picking a
  // file also syncs the cross-page provider selection so the operator's
  // pick surfaces elsewhere.
  function selectFile(id: string) {
    const f = liveFiles.find(x => x.id === id);
    if (!f || f.kind !== 'image' || !hasRealThumb(f)) setActiveLeaf('full');
    setFocusedId(id);
    if (f) {
      if (f.kind === 'image') {
        imageFiles.setSelectedId(id);
      } else if (f.kind === 'aii') {
        aiiFiles.setSelectedId(id);
        setLastTouchedFlatKind('aii');
      } else {
        magFiles.setSelectedId(id);
        setLastTouchedFlatKind('mag');
      }
    }
  }

  return (
    <div
      className="flex-1 flex flex-col overflow-hidden"
      style={{ backgroundColor: colors.bgApp, color: colors.textPrimary }}
    >
      <Topbar filter={filter} onFilter={setFilter} />

      {/* Picker on the LEFT, read-only preview fills the rest. */}
      <div className="flex-1 flex overflow-hidden min-h-0 p-3 gap-3">
        <Picker
          files={pickerFiles}
          focusedId={focusedId}
          onSelect={selectFile}
          onDelete={id => {
            const f = liveFiles.find(x => x.id === id);
            if (f) setPendingDelete({ file: f });
          }}
          search={search}
          onSearch={setSearch}
          collapsed={pickerCollapsed}
          onToggleCollapsed={() => setPickerCollapsed(c => !c)}
        />
        <FocusArea
          file={focused}
          activeLeaf={activeLeaf}
          onLeafChange={setActiveLeaf}
          focusedLeaf={focusedLeaf}
          focusedImageLeaf={focusedImageLeaf}
          imagePreviewVersion={imageFiles.previewVersion}
        />
      </div>

      <div className="px-3 pb-3 shrink-0">
        <ActivityTail
          rows={liveActivity}
          expanded={activityExpanded}
          onToggle={() => setActivityExpanded(v => !v)}
        />
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Forget local copy?"
        detail={pendingDelete
          ? `Removes the ground-station's accumulated chunks for ${fileName(pendingDelete.file)} (${pendingDelete.file.source}). The on-board copy is unaffected. This cannot be undone.`
          : ''}
        variant="destructive"
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (!pendingDelete) return;
          const { file } = pendingDelete;
          void (async () => {
            try {
              // Image pairs are TWO files on disk (full + tn_*); aii/mag
              // are one. ImagePair.stem already carries the extension, so
              // use it verbatim for the full leaf and prefix for the thumb.
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
              showToast(`Forgot local ${fileName(file)}`, 'success');
              setPendingDelete(prev => (prev?.file.id === file.id ? null : prev));
            } catch (err) {
              showToast(`Local delete failed: ${(err as Error).message}`, 'error');
            }
          })();
        }}
      />
    </div>
  );
}

// Standard GSS panel shell — rounded border, panel bg, optional shadow.
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
function PanelTitleBar({
  icon: Icon, title, sub, right,
}: { icon: LucideIcon; title: string; sub?: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div
      className="flex items-center gap-2 px-3 border-b shrink-0"
      style={{ borderColor: colors.borderSubtle, minHeight: 34, paddingTop: 6, paddingBottom: 6 }}
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
  filter, onFilter,
}: {
  filter: FilterKind; onFilter: (f: FilterKind) => void;
}) {
  return (
    <div
      className="flex items-center gap-2 px-3 border-b shrink-0"
      style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel, height: 30 }}
    >
      <Download className="size-3.5 shrink-0" style={{ color: colors.dim }} />
      <span
        className="font-bold uppercase shrink-0"
        style={{ color: colors.value, fontSize: 13, letterSpacing: '0.02em' }}
      >
        Downlink
      </span>

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

      <Eye className="size-3.5 shrink-0" />
      <span className="font-mono text-[11px]" style={{ color: colors.sep }}>
        read-only · command via TX pane
      </span>
    </div>
  );
}

// Small eye glyph for the read-only marker. Inlined to avoid pulling
// another lucide icon for a single 11px decoration.
function Eye({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke={colors.sep} strokeWidth={2} className={className} aria-hidden>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

// ─── PICKER ──────────────────────────────────────────────────────────

const PICKER_WIDTH_EXPANDED = 230;
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
    // Picker lives on the LEFT, so the expand chevron points RIGHT
    // (toward the preview the picker grows into).
    return (
      <PanelShell className="shrink-0" style={{ width: PICKER_WIDTH_COLLAPSED }}>
        <button
          onClick={onToggleCollapsed}
          className="flex items-center justify-center btn-feedback shrink-0 border-b"
          style={{ height: 34, color: colors.dim, borderColor: colors.borderSubtle }}
          title="Expand file picker"
        >
          <ChevronRight className="size-3.5" />
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
            <ChevronLeft className="size-3.5" />
          </button>
        }
      />
      <div className="px-2 py-1.5 border-b shrink-0" style={{ borderColor: colors.borderSubtle }}>
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
  // State symbol duplicates the color encoding for HFDS 9.3.6.
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
      <button
        onClick={(e) => { e.stopPropagation(); onDelete(); }}
        className="shrink-0 inline-flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
        style={{ width: 18, height: 18, color: colors.danger }}
        title={`Forget local copy of ${fileName(file)}`}
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

// ─── FOCUS AREA ──────────────────────────────────────────────────────

function FocusArea({
  file, focusedLeaf, focusedImageLeaf, imagePreviewVersion,
  activeLeaf, onLeafChange,
}: {
  file: DFile | null;
  focusedLeaf: FileLeaf | null;
  focusedImageLeaf: FileLeaf | null;
  imagePreviewVersion: string;
  activeLeaf: Leaf;
  onLeafChange: (l: Leaf) => void;
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
        />
      ) : file.kind === 'aii' ? (
        <AiiFocus file={file} leaf={focusedLeaf} />
      ) : (
        <MagFocus file={file} leaf={focusedLeaf} />
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

// Dedup pass: the header carries identity only (kind · leaf · name ·
// source · download). Chunk counts / size / percent all live in the
// ChunkMap below, so they are no longer repeated here.
function FocusHeader({
  file, activeLeaf, onLeafChange,
}: {
  file: DFile;
  activeLeaf: Leaf;
  onLeafChange: (l: Leaf) => void;
}) {
  const showLeafToggle = file.kind === 'image' && hasRealThumb(file as ImageFile);
  const name = leafFilename(file, activeLeaf);
  return (
    <div
      className="flex items-center gap-2 px-3 border-b shrink-0"
      style={{ borderColor: colors.borderSubtle, minHeight: 34, paddingTop: 6, paddingBottom: 6, overflow: 'hidden' }}
    >
      <KindGlyph kind={file.kind} />
      {showLeafToggle && (
        <LeafToggleInline file={file as ImageFile} activeLeaf={activeLeaf} onLeafChange={onLeafChange} />
      )}
      <span
        className="font-bold shrink-0 truncate"
        style={{ color: colors.value, fontSize: 14, letterSpacing: '0.02em' }}
        title={name}
      >
        {name}
      </span>
      <span className="text-[11px] font-mono truncate" style={{ color: colors.dim }}>{file.source}</span>
      <a
        href={filesEndpoint('preview', file.kind, name, file.source)}
        download={name}
        className="ml-auto inline-flex items-center gap-1.5 px-2 rounded-sm border font-mono text-[11px] btn-feedback shrink-0"
        style={{ height: 20, color: colors.dim, borderColor: colors.borderSubtle, textDecoration: 'none' }}
        title="Download the assembled file (GET — not a command)"
      >
        <Download className="size-3" />download
      </a>
    </div>
  );
}

// Compact FULL/THUMB toggle inline in the FocusHeader.
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
      <LeafButton active={activeLeaf === 'full'} tone={colors.active} label="FULL" onClick={() => onLeafChange('full')} />
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

// — IMAGE FOCUS — progressive preview + meta sidebar + chunk map -------

function ImageFocus({
  file, activeLeaf, imageLeaf, imagePreviewVersion,
}: {
  file: ImageFile;
  activeLeaf: Leaf;
  imageLeaf: FileLeaf | null;
  imagePreviewVersion: string;
}) {
  const leaf = leafTotals(file, activeLeaf);
  const target: ChunkSetTarget | null = imageLeaf
    ? {
        kind: 'image',
        filename: imageLeaf.filename,
        source: imageLeaf.source,
        total: imageLeaf.total,
        received: imageLeaf.received,
      }
    : null;
  // The image is the hero — centered and scaled to fit, preserving its
  // fixed 640×480 (4:3) ratio so the frame never reflows during a
  // partial decode. The chunk map sits beneath it as a compact strip —
  // same bottom slot as the AII/MAG panes for cross-kind consistency.
  return (
    <div className="flex-1 flex flex-col min-h-0 p-3 gap-2">
      <div
        className="flex-1 min-h-0 flex items-center justify-center rounded border overflow-hidden"
        style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgApp }}
      >
        <ProgressivePreview leaf={imageLeaf} version={imagePreviewVersion} />
      </div>
      <ChunkMap leaf={leaf} target={target} tone={activeLeaf === 'thumb' ? colors.warning : colors.active} />
    </div>
  );
}

function ProgressivePreview({
  leaf, version,
}: {
  leaf: FileLeaf | null;
  version: string;
}) {
  // Fetch the (partially-)assembled JPEG; version cache-busts so the <img>
  // reloads on every chunk arrival. Browsers decode top-down, so the image
  // grows visibly as chunks land. The image is always 640×480, so it
  // scales to fit its centered frame without distorting the layout.
  const imgSrc = useMemo(() => {
    if (!leaf) return '';
    const endpoint = filesEndpoint('preview', 'image', leaf.filename, leaf.source);
    const sep = endpoint.includes('?') ? '&' : '?';
    return `${endpoint}${sep}v=${encodeURIComponent(String(version))}`;
  }, [leaf, version]);
  if (!imgSrc) return null;
  return (
    <img
      src={imgSrc}
      alt={leaf?.filename ?? ''}
      style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }}
      onError={() => {}}
    />
  );
}

// ─── CHUNK MAP ───────────────────────────────────────────────────────
// Per-chunk green/red dots: green = received, red = missing. The sole
// home for transfer counts (received / missing / percent / chunk size).
// Above DOT_CAP chunks — or when the received-index fetch hasn't resolved
// yet so per-chunk positions are unknown — it falls back to a run-length
// progress bar with hatched missing segments so a multi-thousand-chunk
// image neither sprays thousands of nodes nor mislabels positions.

const DOT_CAP = 1536;
const MAX_RANGE_CHIPS = 8;

function ChunkMap({
  leaf, target, tone,
}: {
  leaf: LeafData;
  /** Identity for the received-chunk index fetch. `null` when there's no
   *  resolved file. */
  target: ChunkSetTarget | null;
  tone: string;
}) {
  const receivedSet = useFileChunkSet(target);
  const { received, total, chunkSize } = leaf;
  const noTotal = total === 0;
  const complete = !noTotal && received >= total;
  const pctv = pct(received, total);
  const missingCount = Math.max(0, total - received);
  const ranges = noTotal
    ? []
    : computeMissingRanges(total, receivedSet).map(r => [r.start, r.end] as [number, number]);
  const visibleRanges = ranges.slice(0, MAX_RANGE_CHIPS);
  const hiddenCount = ranges.length - visibleRanges.length;
  // Dots only when the count is bounded AND we know which chunks arrived
  // (set populated, or nothing received yet). Otherwise the run-length bar.
  const positionsKnown = receivedSet.size > 0 || received === 0;
  const useDots = !noTotal && total <= DOT_CAP && positionsKnown;
  return (
    <div className="shrink-0 rounded-md border p-2" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
      <div className="flex items-center gap-3 mb-1.5 flex-wrap">
        <span
          className="font-bold uppercase"
          style={{ color: colors.value, fontSize: 11, letterSpacing: '0.08em' }}
        >
          Chunk map
        </span>
        <span className="font-mono text-[11px]" style={{ color: colors.dim }}>
          {noTotal ? (
            <span style={{ color: colors.info }}>count required</span>
          ) : (
            <>
              {total} chunks · {chunkSize} B · <span style={{ color: colors.value }}>{pctv}%</span>
            </>
          )}
        </span>
        <div className="flex-1" />
        {!noTotal && (
          <>
            <LegendDot color={colors.success} label="received" value={received} />
            <LegendDot color={colors.danger} label="missing" value={missingCount} />
          </>
        )}
      </div>

      {noTotal ? (
        <div
          className="rounded-sm"
          style={{
            height: 16,
            border: `1px solid ${colors.dim}55`,
            backgroundImage: `repeating-linear-gradient(45deg, ${colors.dim}22 0 4px, transparent 4px 8px)`,
          }}
        />
      ) : useDots ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, 6px)', gap: 2 }}>
          {Array.from({ length: total }, (_, i) => {
            const got = receivedSet.has(i);
            return (
              <div
                key={i}
                title={`chunk ${i} · ${got ? 'received' : 'missing'}`}
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: '50%',
                  backgroundColor: got ? colors.success : colors.danger,
                  opacity: got ? 1 : 0.82,
                }}
              />
            );
          })}
        </div>
      ) : (
        <div
          className="relative w-full rounded-sm overflow-hidden"
          style={{
            height: 16,
            backgroundColor: complete ? `${colors.success}22` : `${tone}1A`,
            border: `1px solid ${complete ? `${colors.success}55` : `${tone}55`}`,
          }}
        >
          {!complete && (
            <div style={{ position: 'absolute', inset: 0, width: `${pctv}%`, backgroundColor: tone, boxShadow: `0 0 8px ${tone}66`, transition: 'width 240ms ease' }} />
          )}
          {complete && <div style={{ position: 'absolute', inset: 0, backgroundColor: colors.success }} />}
          {ranges.map(([lo, hi], i) => {
            const left = (lo / total) * 100;
            const width = ((hi - lo + 1) / total) * 100;
            return (
              <div
                key={i}
                style={{
                  position: 'absolute',
                  left: `${left}%`,
                  width: `${width}%`,
                  top: 0, bottom: 0,
                  background: `repeating-linear-gradient(45deg, ${colors.danger}55 0 4px, transparent 4px 8px)`,
                  borderLeft: `1px solid ${colors.danger}99`,
                  borderRight: `1px solid ${colors.danger}99`,
                }}
                title={`missing ${lo}–${hi}`}
              />
            );
          })}
        </div>
      )}

      {!noTotal && !complete && ranges.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5 font-mono text-[11px] items-center">
          <span style={{ color: colors.dim }}>missing ranges</span>
          {visibleRanges.map(([lo, hi], i) => (
            <span
              key={i}
              className="px-1.5 rounded-sm tabular-nums"
              style={{ color: colors.danger, backgroundColor: colors.dangerFill, border: `1px solid ${colors.danger}55` }}
              title={`missing ${lo}–${hi}`}
            >
              {lo}–{hi}
            </span>
          ))}
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

function LegendDot({ color, label, value }: { color: string; label: string; value: number }) {
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[11px]" style={{ color: colors.dim }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', backgroundColor: color, display: 'inline-block' }} />
      {label} <span style={{ color: colors.value }}>{value}</span>
    </span>
  );
}

// — AII FOCUS ---------------------------------------------------------

function AiiFocus({ file, leaf }: { file: FlatFile; leaf: FileLeaf | null }) {
  const target: ChunkSetTarget | null = leaf
    ? { kind: 'aii', filename: leaf.filename, source: leaf.source, total: leaf.total, received: leaf.received }
    : null;
  // Content pane is the hero (top); chunk map is the compact bottom strip
  // — same slot as the image preview.
  return (
    <div className="flex-1 flex flex-col min-h-0 p-3 gap-2">
      <div className="flex-1 min-h-0 rounded-md border overflow-hidden" style={{ borderColor: colors.borderSubtle }}>
        <JsonPreview file={leaf} />
      </div>
      <ChunkMap
        leaf={{ received: file.received, total: file.total, chunkSize: file.chunkSize }}
        target={target}
        tone={colors.info}
      />
    </div>
  );
}

// — MAG FOCUS — reuse existing MagPreview metadata + download anchor.

function MagFocus({ file, leaf }: { file: FlatFile; leaf: FileLeaf | null }) {
  const target: ChunkSetTarget | null = leaf
    ? { kind: 'mag', filename: leaf.filename, source: leaf.source, total: leaf.total, received: leaf.received }
    : null;
  return (
    <div className="flex-1 flex flex-col min-h-0 p-3 gap-2">
      <div className="flex-1 min-h-0 rounded-md border overflow-hidden" style={{ borderColor: colors.borderSubtle, backgroundColor: colors.bgPanel }}>
        <MagPreview file={leaf} />
      </div>
      <ChunkMap
        leaf={{ received: file.received, total: file.total, chunkSize: file.chunkSize }}
        target={target}
        tone={colors.info}
      />
    </div>
  );
}

// ─── ACTIVITY TAIL ───────────────────────────────────────────────────

function ActivityTail({
  rows, expanded, onToggle,
}: { rows: ReadonlyArray<ActivityRow>; expanded: boolean; onToggle: () => void }) {
  // "Receiving" is a windowed signal — true only while a new file-event
  // packet arrived in the last RECEIVING_WINDOW_MS. Dep is `last?.tsRel`,
  // NOT `rows.length` — `liveActivity` is `slice(-50)` so once 50 packets
  // have landed `length` saturates; the latest packet's relative
  // timestamp still changes.
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
