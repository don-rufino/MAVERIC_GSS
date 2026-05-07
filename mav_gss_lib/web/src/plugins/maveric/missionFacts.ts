import type { MissionFacts, RxPacket } from '@/lib/types'
import type { FileKindCaps } from './shared/fileKinds'

export interface MavericMissionFacts extends MissionFacts {
  id: 'maveric'
  facts: {
    header?: {
      src?: string | number
      dest?: string | number
      echo?: string | number
      ptype?: string | number
      [key: string]: unknown
    }
    protocol?: Record<string, unknown>
    integrity?: Record<string, unknown>
  }
}

export function mavericFacts(packet: RxPacket): MavericMissionFacts | null {
  return packet.mission?.id === 'maveric' ? packet.mission as MavericMissionFacts : null
}

export function mavericHeader(packet: RxPacket): MavericMissionFacts['facts']['header'] {
  return mavericFacts(packet)?.facts?.header ?? {}
}

export function mavericCmdId(packet: RxPacket): string {
  return String(packet.mission?.cmd_id ?? '')
}

export function mavericPtype(packet: RxPacket): string {
  return String(mavericHeader(packet)?.ptype ?? '')
}

export function mavericSrc(packet: RxPacket): string {
  return String(mavericHeader(packet)?.src ?? '')
}

const ERROR_PTYPES = new Set(['ERR', 'NACK', 'FAIL', 'TIMEOUT'])

/** Match an RX packet against a file kind's capability profile.
 *  Predicate is two-part: (1) cmd_id family match via `caps.rxFilter`,
 *  and (2) optional error-ptype fallback for nodes listed in
 *  `caps.errorNodes`. Image opts in to the fallback (preserving legacy
 *  isImagingRxPacket behavior); AII/MAG opt out. */
export function isFileKindRxPacket(p: RxPacket, caps: FileKindCaps): boolean {
  const cmdRaw = mavericCmdId(p)
  if (caps.rxFilter.test(cmdRaw)) return true
  if (caps.errorNodes.length === 0) return false
  const ptype = mavericPtype(p).toUpperCase()
  if (!ERROR_PTYPES.has(ptype)) return false
  const node = mavericSrc(p)
  return caps.errorNodes.includes(node)
}

