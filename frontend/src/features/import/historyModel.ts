import type { Schemas } from '../../api/client'

export type Group = Schemas['GmailGroup']

export type CaseBucket = { id: string; title: string; groups: Group[] }

export type Filter = 'all' | 'case' | 'nocase' | 'pending' | 'done'
export type Sort = 'oldest' | 'recent' | 'pending'

export const UNREFERENCED = 'unreferenced'

/** "8372-25" is stored as the case id; the lawyer's file number reads "8372/25". */
export function referenceLabel(group: Group) {
  if (group.key === UNREFERENCED) return 'No reference'
  return group.kind === 'internal_id' ? group.key.replace('-', '/') : group.key
}

/** "Hansen ./. Liu - wg. Sorge" → parties "Hansen ./. Liu", matter "wg. Sorge". */
export function splitTopic(topic: string | null | undefined) {
  const text = (topic ?? '').trim()
  const match = /^(.*?)\s+[-–]\s+(?:[-–]\s+)*(.+)$/.exec(text)
  if (!match) return { parties: '', matter: text }
  return { parties: (match[1] ?? '').trim(), matter: (match[2] ?? '').trim() }
}

export type Sections = { cases: CaseBucket[]; noCase: Group[]; unreferenced?: Group }

/** Referenced groups split by whether a case exists; groups sharing a case nest together. */
export function sectionGroups(groups: Group[]): Sections {
  const buckets = new Map<string, CaseBucket>()
  const noCase: Group[] = []
  let unreferenced: Group | undefined
  for (const g of groups) {
    if (g.key === UNREFERENCED) unreferenced = g
    else if (g.matched_case_id) {
      const bucket = buckets.get(g.matched_case_id) ?? {
        id: g.matched_case_id,
        title: g.matched_case_title ?? '',
        groups: [],
      }
      bucket.groups.push(g)
      buckets.set(g.matched_case_id, bucket)
    } else noCase.push(g)
  }
  return { cases: [...buckets.values()], noCase, unreferenced }
}

export type Totals = { count: number; ingested: number; first: string; last: string }

export function groupTotals(groups: Group[]): Totals {
  const firsts = groups.map((g) => g.first_at).sort()
  const lasts = groups.map((g) => g.last_at).sort()
  return {
    count: groups.reduce((sum, g) => sum + g.count, 0),
    ingested: groups.reduce((sum, g) => sum + g.ingested_count, 0),
    first: firsts[0] ?? '',
    last: lasts[lasts.length - 1] ?? '',
  }
}

export const isDone = (g: { count: number; ingested_count: number }) => g.ingested_count >= g.count

export function filterGroups(groups: Group[], opts: { query: string; filter: Filter }) {
  const q = opts.query.trim().toLowerCase()
  return groups.filter((g) => {
    if (opts.filter === 'case' && !g.matched_case_id) return false
    if (opts.filter === 'nocase' && g.matched_case_id) return false
    if (opts.filter === 'pending' && isDone(g)) return false
    if (opts.filter === 'done' && !isDone(g)) return false
    if (!q) return true
    return [referenceLabel(g), g.key, g.topic, g.matched_case_id, g.matched_case_title].some(
      (field) => field?.toLowerCase().includes(q),
    )
  })
}

type Sortable = Pick<Group, 'count' | 'ingested_count' | 'first_at' | 'last_at'>

/** Stable comparator for a section's rows (groups or case totals). */
export function compareBy(sort: Sort) {
  return (a: Sortable, b: Sortable) => {
    if (sort === 'recent') return b.last_at.localeCompare(a.last_at)
    if (sort === 'pending') {
      return b.count - b.ingested_count - (a.count - a.ingested_count)
    }
    return a.first_at.localeCompare(b.first_at)
  }
}

export function sortGroups(groups: Group[], sort: Sort) {
  return [...groups].sort(compareBy(sort))
}

/** A bucket sorts by its combined totals; its references sort among themselves. */
export function sortSections(sections: Sections, sort: Sort): Sections {
  const cmp = compareBy(sort)
  const totals = (b: CaseBucket) => {
    const t = groupTotals(b.groups)
    return { count: t.count, ingested_count: t.ingested, first_at: t.first, last_at: t.last }
  }
  return {
    cases: sections.cases
      .map((b) => ({ ...b, groups: sortGroups(b.groups, sort) }))
      .sort((a, b) => cmp(totals(a), totals(b))),
    noCase: sortGroups(sections.noCase, sort),
    unreferenced: sections.unreferenced,
  }
}

export type TimeAxis = {
  /** Position of an ISO timestamp on the axis, 0..1. */
  pos: (iso: string) => number
  years: { year: number; at: number }[]
}

/** One shared axis over every group's span, from the first of Jan to the last of Dec. */
export function timeAxis(groups: Group[]): TimeAxis {
  if (groups.length === 0) return { pos: () => 0, years: [] }
  const t = groupTotals(groups)
  const firstYear = new Date(t.first).getFullYear()
  const lastYear = new Date(t.last).getFullYear()
  const min = new Date(firstYear, 0, 1).getTime()
  const max = new Date(lastYear + 1, 0, 1).getTime()
  const pos = (iso: string) => (new Date(iso).getTime() - min) / (max - min)
  const years = []
  for (let year = firstYear; year <= lastYear; year++) {
    years.push({ year, at: (new Date(year, 0, 1).getTime() - min) / (max - min) })
  }
  return { pos, years }
}
