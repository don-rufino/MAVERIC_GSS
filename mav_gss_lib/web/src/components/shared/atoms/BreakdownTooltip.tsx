import { Fragment } from 'react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import type { BreakdownRow } from '@/lib/frequencyOffset'

/** Hover breakdown for a compact list value (label/value rows, optional note). */
export function BreakdownTooltip({ rows, note, children }: {
  rows: BreakdownRow[]
  note?: string
  children: React.ReactNode
}) {
  return (
    <TooltipProvider delay={300}>
      <Tooltip>
        <TooltipTrigger render={<span />}>{children}</TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          <span className="grid gap-1">
            <dl className="grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5 m-0">
              {rows.map(r => (
                <Fragment key={r.label}>
                  <dt className={`opacity-70 ${r.sub ? 'pl-3' : ''}`}>{r.sub ? '└ ' : ''}{r.label}</dt>
                  <dd className="m-0 font-mono tabular-nums text-right">{r.value}</dd>
                </Fragment>
              ))}
            </dl>
            {note && <span className="opacity-70">{note}</span>}
          </span>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
