import { useContext } from 'react'
import { colors } from '@/lib/colors'
import { GssInput } from '@/components/ui/gss-input'
import { Switch } from '@/components/ui/switch'
import { RefreshCw } from 'lucide-react'
import { SearchContext, matchesQuery, useFieldVisible } from './search'

export type Control =
  | { kind: 'text'; value: string; onChange: (v: string) => void; stacked?: boolean }
  | { kind: 'number'; value: number; onChange: (v: number) => void; unit?: string }
  | { kind: 'toggle'; value: boolean; onChange: (v: boolean) => void }
  | { kind: 'info'; value: string }
  | { kind: 'tle'; draft: string; onChange: (v: string) => void }
  | {
      kind: 'fetch-identifier'
      value: string
      onChange: (v: string) => void
      onFetch: () => void
      fetching: boolean
      fetchMsg: string
      disabled: boolean
    }

export interface SettingRow { id: string; label: string; description?: string; control: Control }
export interface SettingGroup { title: string; rows: SettingRow[] }
export interface SettingPane { id: string; title: string; description?: string; groups: SettingGroup[] }

function isStacked(c: Control): boolean {
  return (c.kind === 'text' && !!c.stacked) || c.kind === 'tle' || c.kind === 'fetch-identifier'
}

function TleBlock({ draft, onChange }: { draft: string; onChange: (v: string) => void }) {
  return (
    <div className="flex rounded-md overflow-hidden" style={{ backgroundColor: colors.bgApp, border: `1px solid ${colors.borderSubtle}` }}>
      <div
        className="shrink-0 w-7 text-center py-2 font-mono leading-[1.85] select-none"
        style={{ fontSize: '10.5px', color: colors.sep, backgroundColor: colors.bgPanel, borderRight: `1px solid ${colors.borderSubtle}` }}
      >
        ·<br />1<br />2
      </div>
      <textarea
        value={draft}
        onChange={(e) => onChange(e.target.value)}
        rows={3}
        spellCheck={false}
        placeholder={'SATNAME\n1 NNNNNU ...\n2 NNNNN ...'}
        className="w-full font-mono leading-[1.85] px-2.5 py-2 resize-y bg-transparent outline-none"
        style={{ fontSize: '11px', color: colors.value, letterSpacing: '-0.2px' }}
      />
    </div>
  )
}

function FetchIdentifier({ value, onChange, onFetch, fetching, fetchMsg, disabled }: Extract<Control, { kind: 'fetch-identifier' }>) {
  return (
    <div>
      <div className="flex gap-2">
        <GssInput className="flex-1" value={value} onChange={(e) => onChange(e.target.value)} />
        <button
          type="button"
          disabled={disabled}
          onClick={onFetch}
          className="text-xs rounded px-3.5 inline-flex items-center gap-1.5 whitespace-nowrap disabled:opacity-40"
          style={{ border: `1px solid ${colors.active}4D`, color: colors.active }}
        >
          <RefreshCw className="size-3.5" />
          {fetching ? 'Fetching…' : 'Fetch'}
        </button>
      </div>
      {fetchMsg && <div className="mt-1.5" style={{ fontSize: '11.5px', color: colors.dim }}>{fetchMsg}</div>}
    </div>
  )
}

function ControlView({ control }: { control: Control }) {
  switch (control.kind) {
    case 'text':
      return control.stacked ? (
        <GssInput className="w-full" style={{ textAlign: 'left' }} value={control.value} onChange={(e) => control.onChange(e.target.value)} />
      ) : (
        <GssInput className="w-36 text-right" value={control.value} onChange={(e) => control.onChange(e.target.value)} />
      )
    case 'number':
      return (
        <div className="flex items-center gap-1.5">
          <GssInput type="number" className="w-16 text-right" value={control.value} onChange={(e) => control.onChange(Number(e.target.value))} />
          {control.unit && <span className="text-xs" style={{ color: colors.dim }}>{control.unit}</span>}
        </div>
      )
    case 'toggle':
      return (
        <Switch
          checked={control.value}
          onCheckedChange={(checked) => control.onChange(checked)}
          className="data-checked:bg-[#3CC98E]"
        />
      )
    case 'info':
      return <span className="font-mono text-xs text-right break-all" style={{ color: colors.value }}>{control.value}</span>
    case 'tle':
      return <TleBlock draft={control.draft} onChange={control.onChange} />
    case 'fetch-identifier':
      return <FetchIdentifier {...control} />
  }
}

export function RowRenderer({ row }: { row: SettingRow }) {
  const visible = useFieldVisible(row.label, row.description)
  if (!visible) return null
  const stacked = isStacked(row.control)
  const labelBlock = (
    <div className={stacked ? '' : 'min-w-0'}>
      <div className="text-[13px]" style={{ color: colors.value }}>{row.label}</div>
      {row.description && <div className="mt-0.5" style={{ fontSize: '11.5px', color: colors.dim }}>{row.description}</div>}
    </div>
  )
  if (stacked) {
    return (
      <div className="py-1.5">
        {labelBlock}
        <div className="mt-1.5"><ControlView control={row.control} /></div>
      </div>
    )
  }
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      {labelBlock}
      <ControlView control={row.control} />
    </div>
  )
}

export function GroupRenderer({ group }: { group: SettingGroup }) {
  const query = useContext(SearchContext)
  const anyVisible = group.rows.some((r) => matchesQuery(query, r.label, r.description))
  if (!anyVisible) return null
  return (
    <div className="mt-4">
      <div className="text-[11px] font-bold uppercase tracking-wider mb-2" style={{ color: colors.dim }}>{group.title}</div>
      {group.rows.map((r) => <RowRenderer key={r.id} row={r} />)}
    </div>
  )
}

export function PaneRenderer({ pane }: { pane: SettingPane }) {
  const query = useContext(SearchContext)
  const visible = pane.groups.some((g) => g.rows.some((r) => matchesQuery(query, r.label, r.description)))
  if (!visible) return null
  return (
    <div className="mb-2">
      <div className="text-[15px] font-semibold" style={{ color: colors.value }}>{pane.title}</div>
      {pane.description && <div className="text-xs mt-0.5" style={{ color: colors.dim }}>{pane.description}</div>}
      {pane.groups.map((g) => <GroupRenderer key={g.title} group={g} />)}
    </div>
  )
}
