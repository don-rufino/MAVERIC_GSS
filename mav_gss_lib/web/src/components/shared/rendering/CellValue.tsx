import { Badge } from '@/components/ui/badge'
import { colors } from '@/lib/colors'
import { ValueBadge } from '@/components/shared/atoms/ValueBadge'
import { columnWidthClass, columnWidthStyle, columnAlignClass } from '@/lib/columns'
import type { ColumnDef, RenderCell, RenderingFlag } from '@/lib/types'

function iconTokenForValue(
  value: unknown,
  valueIcons: Record<string, string> | undefined,
  defaultIcon: string | undefined,
): string | undefined {
  if (!valueIcons && !defaultIcon) return undefined
  const label = String(value ?? '')
  return valueIcons?.[label] ?? defaultIcon
}

// Pure dispatcher driven by cell properties — no platform-id awareness.
// Cells decide their own appearance via `flags` (array → flag badges),
// `badge` (truthy → ValueBadge), `tone`/`tabular`/`suffix`/`monospace`
// (text-cell variants).
export function CellValue({ col: c, row, showFrame, showEcho }: {
  col: ColumnDef
  row: Record<string, RenderCell>
  showFrame: boolean
  showEcho: boolean
}) {
  if (c.toggle === 'showFrame' && !showFrame) return null
  if (c.toggle === 'showEcho' && !showEcho) return null

  const cell = row[c.id]
  const val = cell?.value
  const width = columnWidthClass(c)
  const widthStyle = columnWidthStyle(c)
  const align = columnAlignClass(c)

  if (Array.isArray(val)) {
    const flags = val as RenderingFlag[]
    return (
      <span className={`py-1 px-1 ${width} ${align}`} style={widthStyle}>
        <span className="flex items-center gap-1 justify-end whitespace-nowrap">
          {flags.map((f, i) => (
            <Badge key={i} variant={f.tone === 'danger' ? 'destructive' : 'secondary'} className="text-[11px] px-1 py-0 h-5"
              style={f.tone !== 'danger' ? { backgroundColor: `${f.tone === 'warning' ? colors.warning : colors.ulColor}22`, color: f.tone === 'warning' ? colors.warning : colors.ulColor } : undefined}>
              {f.tag}
            </Badge>
          ))}
        </span>
      </span>
    )
  }

  if (cell?.badge) {
    return (
      <span className={`py-1 px-1 ${width}`} style={widthStyle}>
        <ValueBadge
          value={val as string | number}
          tone={cell.tone}
          iconToken={iconTokenForValue(val, c.value_icons, c.default_icon)}
        />
      </span>
    )
  }

  const text = `${val ?? ''}${cell?.suffix ?? ''}`
  const autoTitle = c.truncate && !cell?.tooltip ? text : undefined
  return (
    <span
      className={`py-1 px-1 ${width} ${align} whitespace-nowrap ${cell?.monospace ? 'font-mono' : ''} ${cell?.tabular ? 'tabular-nums' : ''}`}
      style={{ color: cell?.tone ?? colors.dim, ...widthStyle }}
      title={cell?.tooltip ?? autoTitle}
    >
      {text}
    </span>
  )
}
