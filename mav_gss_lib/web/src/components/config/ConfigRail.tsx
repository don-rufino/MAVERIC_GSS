import type { ComponentType } from 'react'
import { Search } from 'lucide-react'
import { colors } from '@/lib/colors'

export interface RailItem {
  id: string
  label: string
  group: string
  Icon: ComponentType<{ className?: string }>
}

interface ConfigRailProps {
  items: RailItem[]
  activeId: string
  searching: boolean
  onSelect: (id: string) => void
  search: string
  onSearch: (v: string) => void
}

export function ConfigRail({ items, activeId, searching, onSelect, search, onSearch }: ConfigRailProps) {
  const groups = items.reduce<string[]>((acc, i) => (acc.includes(i.group) ? acc : [...acc, i.group]), [])
  return (
    <nav
      aria-label="Settings categories"
      className="w-[170px] shrink-0 overflow-y-auto p-2.5 border-r"
      style={{ backgroundColor: colors.bgPanel, borderColor: colors.borderSubtle }}
    >
      <div className="flex items-center gap-2 rounded-md px-2 py-1.5" style={{ backgroundColor: colors.bgApp, border: `1px solid ${colors.borderSubtle}` }}>
        <Search className="size-3.5 shrink-0" style={{ color: colors.sep }} />
        <input
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Search settings"
          className="w-full bg-transparent outline-none text-xs"
          style={{ color: colors.value }}
        />
      </div>
      {groups.map((g) => (
        <div key={g}>
          <div className="text-[11px] font-semibold uppercase tracking-wider mt-3.5 mb-1.5 ml-1.5" style={{ color: colors.sep }}>{g}</div>
          {items.filter((i) => i.group === g).map((item) => {
            const active = !searching && item.id === activeId
            return (
              <button
                key={item.id}
                onClick={() => onSelect(item.id)}
                className="flex w-full items-center gap-2.5 rounded px-2.5 py-1.5 text-left text-[12.5px] color-transition"
                style={{
                  backgroundColor: active ? colors.activeFill : 'transparent',
                  color: active ? colors.active : colors.textSecondary,
                  borderLeft: `2px solid ${active ? colors.active : 'transparent'}`,
                }}
              >
                <item.Icon className="size-[15px]" />
                {item.label}
              </button>
            )
          })}
        </div>
      ))}
    </nav>
  )
}
