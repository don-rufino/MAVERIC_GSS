import { Separator } from '@/components/ui/separator'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { Clock, Radio, AlertTriangle, Binary } from 'lucide-react'
import { colors, frameColor } from '@/lib/colors'
import { SemanticBlocks, ProtocolBlocks, IntegritySection } from '@/components/shared/rendering'
import { integrityBlocks, missionDetailBlocks, parameterBlocks, protocolBlocks, rxTime } from '@/lib/rxPacket'
import type { ParamUpdate, RxPacket } from '@/lib/types'

interface PacketDetailProps {
  packet: RxPacket
  parameters: ParamUpdate[]
  showHex: boolean
  showWrapper: boolean
  showFrame: boolean
  /** Per-beacon Signal card (rendered by the caller, which knows the pass). */
  signalCard?: React.ReactNode
}

function F({ icon: Icon, label, value, color, tooltip }: { icon?: React.ElementType; label: string; value: string; color?: string; tooltip?: string }) {
  const valueEl = <span style={{ color: color ?? colors.value }}>{value}</span>
  return (
    <span className="inline-flex items-center gap-1 text-xs whitespace-nowrap">
      {Icon && <Icon className="size-3" style={{ color: colors.sep }} />}
      <span style={{ color: colors.sep }}>{label}:</span>
      {tooltip ? (
        <TooltipProvider delay={300}>
          <Tooltip>
            <TooltipTrigger render={<span />} style={{ color: color ?? colors.value }}>{value}</TooltipTrigger>
            <TooltipContent side="top" className="text-xs">{tooltip}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      ) : valueEl}
    </span>
  )
}

export function PacketDetail({ packet: p, parameters, showHex, showWrapper, showFrame, signalCard }: PacketDetailProps) {
  const detailBlocks = missionDetailBlocks(p)
  const paramBlocks = parameterBlocks(parameters)
  const pBlocks = protocolBlocks(p)
  const iBlocks = integrityBlocks(p)

  return (
    <div className="px-3 py-2 space-y-1.5 border-t font-mono" style={{ borderColor: colors.borderSubtle }}>
      <div className="flex items-center gap-4">
        <F icon={Clock} label="Time" value={rxTime(p)} />
      </div>

      {showFrame && p.frame && (
        <div className="flex items-center gap-4">
          <F icon={Radio} label="Frame" value={p.frame} color={frameColor(p.frame)} />
        </div>
      )}

      <SemanticBlocks blocks={detailBlocks} />

      {paramBlocks.length > 0 && <SemanticBlocks blocks={paramBlocks} />}

      {p.warnings.length > 0 && (
        <div className="flex items-center gap-1">
          <AlertTriangle className="size-3 shrink-0" style={{ color: colors.warning }} />
          {p.warnings.map((w, i) => (
            <Badge key={i} className="text-[11px] h-5" style={{ backgroundColor: `${colors.warning}22`, color: colors.warning }}>
              {w}
            </Badge>
          ))}
        </div>
      )}

      {signalCard && <div className="font-sans pt-1">{signalCard}</div>}

      {showWrapper && (
        <>
          <Separator style={{ backgroundColor: colors.borderSubtle }} />
          <IntegritySection blocks={iBlocks} />
          <ProtocolBlocks blocks={pBlocks} />
        </>
      )}

      {showHex && p.raw_hex && (
        <>
          <Separator style={{ backgroundColor: colors.borderSubtle }} />
          <div className="flex items-start gap-1">
            <Binary className="size-3 mt-0.5 shrink-0" style={{ color: colors.sep }} />
            <pre className="text-[11px] p-2 rounded font-mono flex-1 whitespace-pre-wrap break-all" style={{ color: colors.dim, backgroundColor: colors.bgApp }}>
              {p.raw_hex.match(/.{1,2}/g)?.join(' ')}
            </pre>
          </div>
        </>
      )}
    </div>
  )
}
