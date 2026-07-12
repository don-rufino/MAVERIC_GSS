import { colors } from '@/lib/colors'

/**
 * Classify a GNU Radio log line and pick a color from the HFDS palette
 * (danger / warning / info / active / neutral, plus text-secondary as
 * the unclassified fallback). Hex byte rows render in the active cyan
 * tone so live downlink data stands out from structural chatter.
 */
export function lineColor(line: string): string {
  // Strip the supervisor's UTC "HH:MM:SSZ " prefix. Accept the old local
  // form as well so historical/in-memory lines remain classified correctly.
  const body = line.replace(/^\d{2}:\d{2}:\d{2}Z?\s/, '')

  // GR canonical level brackets.
  if (body.startsWith('[ERROR]') || body.startsWith('gr::log :ERROR')) return colors.danger
  if (body.startsWith('[WARNING]') || body.startsWith('gr::log :WARN')) return colors.warning
  if (body.startsWith('[DEBUG]')) return colors.textMuted
  if (body.startsWith('[INFO]') || body.startsWith('gr::log :INFO')) return colors.info

  // Python tracebacks → danger.
  if (
    body.startsWith('Traceback (') ||
    /^\s*File "/.test(body) ||
    /^[A-Za-z][A-Za-z0-9_.]*Error\b/.test(body) ||
    /^[A-Za-z][A-Za-z0-9_.]*Exception\b/.test(body)
  ) return colors.danger

  // UHD overflow/underflow markers ("O", "U", "OO", ...) frequently bleed
  // into the start of stdout lines — strip them and re-classify the rest.
  const overflowMatch = /^[OU]+/.exec(body)
  const rest = overflowMatch ? body.slice(overflowMatch[0].length) : body

  // "usrp_source :error: In the last X ms, N overflows occurred." — operational caution.
  if (/^\s*usrp_source\s*:error:/.test(rest)) return colors.warning
  // Bare overflow markers on their own line.
  if (overflowMatch && rest.trim() === '') return colors.warning

  // gr-satellites VERBOSE PDU DEBUG PRINT structure.
  // "***** VERBOSE PDU DEBUG PRINT ******" / "************************************"
  if (/^\*{3,}/.test(rest)) return colors.neutral
  // "((transmitter . 9k6 FSK AX100 ASM+Golay downlink))" — frame header.
  if (/^\(\(.*(downlink|uplink)\)\)/.test(rest)) return colors.info
  // "pdu length = ..." / "pdu vector contents = "
  if (/^pdu\s+(length|vector\b)/.test(rest)) return colors.textSecondary
  // "0000: 90 06 00 00 ..." — hex offset followed by bytes (downlink payload).
  if (/^[0-9a-fA-F]{4}:\s/.test(rest)) return colors.active

  return colors.textSecondary
}
