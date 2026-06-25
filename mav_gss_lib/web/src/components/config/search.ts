import { createContext, useContext } from 'react'

/**
 * True when every whitespace-separated term in `query` appears
 * (case-insensitive) somewhere in the combined haystack. A blank query
 * always matches, so normal (non-search) rendering shows every field.
 */
export function matchesQuery(query: string, ...haystack: Array<string | undefined>): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  const hay = haystack.filter(Boolean).join(' ').toLowerCase()
  return q.split(/\s+/).every((term) => hay.includes(term))
}

/** Current search query, provided by ConfigModal. Empty string = not searching. */
export const SearchContext = createContext('')

/** Field rows call this with their label/description to decide whether to render. */
export function useFieldVisible(...texts: Array<string | undefined>): boolean {
  const query = useContext(SearchContext)
  return matchesQuery(query, ...texts)
}
