// Two-line element parsing for the tracking/Doppler config editor.
//
// Operators paste a TLE the way Celestrak/Space-Track distribute it: an
// optional name line followed by the two element lines. `parseTleBlock`
// turns that free-form paste into the discrete `platform.tracking.tle`
// fields the backend stores; `joinTleBlock` renders them back for display.

export interface ParsedTle {
  name?: string
  line1?: string
  line2?: string
}

const ELEMENT_LINE = /^([12]) /

// TLE element lines begin with "1 " / "2 " (line number then a space).
// Anything else non-blank is treated as the satellite name. First
// occurrence of each wins so a stray duplicate can't clobber a good line.
export function parseTleBlock(text: string): ParsedTle {
  const out: ParsedTle = {}
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line) continue
    const element = ELEMENT_LINE.exec(line)
    if (element) {
      const key = element[1] === '1' ? 'line1' : 'line2'
      if (!out[key]) out[key] = line
    } else if (!out.name) {
      out.name = line
    }
  }
  return out
}

export function joinTleBlock(tle: ParsedTle): string {
  return [tle.name, tle.line1, tle.line2].filter((part): part is string => Boolean(part)).join('\n')
}
