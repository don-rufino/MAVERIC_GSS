import { useContext, useEffect, useRef } from 'react'
import { colors } from '@/lib/colors'
import { GssInput } from '@/components/ui/gss-input'
import { Switch } from '@/components/ui/switch'
import { RefreshCw, Lock, CircleCheck, CircleX } from 'lucide-react'
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
  | { kind: 'select'; value: string; options: { value: string; label: string }[]; onChange: (v: string) => void }
  | { kind: 'env-status'; rows: { name: string; present: boolean }[]; hint: string }

export interface SettingRow { id: string; label: string; description?: string; control: Control }
export interface SettingGroup { title: string; rows: SettingRow[] }
export interface SettingPane { id: string; title: string; description?: string; groups: SettingGroup[] }

function isStacked(c: Control): boolean {
  return (c.kind === 'text' && !!c.stacked) || c.kind === 'tle' || c.kind === 'fetch-identifier' || c.kind === 'env-status'
}

function TleBlock({ draft, onChange }: { draft: string; onChange: (v: string) => void }) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [draft])
  return (
    <textarea
      ref={ref}
      value={draft}
      onChange={(e) => onChange(e.target.value)}
      spellCheck={false}
      placeholder={'SATNAME\n1 NNNNNU ...\n2 NNNNN ...'}
      className="w-full font-mono leading-[1.7] rounded-md px-2.5 py-2 outline-none resize-none overflow-hidden"
      style={{ fontSize: '11px', minHeight: '4.5rem', color: colors.value, backgroundColor: colors.bgApp, border: `1px solid ${colors.borderSubtle}`, letterSpacing: '-0.2px' }}
    />
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
    case 'select':
      return (
        <select
          value={control.value}
          onChange={(e) => control.onChange(e.target.value)}
          className="w-40 rounded px-2.5 py-1.5 text-[12.5px] outline-none"
          style={{ backgroundColor: colors.bgApp, border: `1px solid ${colors.borderSubtle}`, color: colors.value }}
        >
          {control.options.map((o) => <option key={o.value} value={o.value} style={{ backgroundColor: colors.bgApp }}>{o.label}</option>)}
        </select>
      )
    case 'env-status':
      return (
        <div className="rounded-md p-3" style={{ backgroundColor: colors.bgPanel, border: `1px solid ${colors.borderSubtle}` }}>
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider mb-2" style={{ color: colors.dim }}>
            <Lock className="size-3" /> Credentials · environment
          </div>
          {control.rows.map((r) => (
            <div key={r.name} className="flex items-center justify-between py-0.5">
              <span className="font-mono text-[11.5px]" style={{ color: colors.textSecondary }}>{r.name}</span>
              <span className="flex items-center gap-1.5 text-xs" style={{ color: r.present ? colors.success : colors.danger }}>
                {r.present ? <CircleCheck className="size-3.5" /> : <CircleX className="size-3.5" />}
                {r.present ? 'present' : 'missing'}
              </span>
            </div>
          ))}
          <div className="text-[11.5px] mt-2 leading-snug" style={{ color: colors.sep }}>{control.hint}</div>
        </div>
      )
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
    const hideLabel = row.control.kind === 'env-status'
    return (
      <div className="py-1.5">
        {!hideLabel && labelBlock}
        <div className={hideLabel ? '' : 'mt-1.5'}><ControlView control={row.control} /></div>
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
