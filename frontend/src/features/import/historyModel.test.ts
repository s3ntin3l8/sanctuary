import { describe, expect, it } from 'vitest'

import {
  type Group,
  filterGroups,
  groupTotals,
  sectionGroups,
  sortGroups,
  sortSections,
  splitTopic,
  timeAxis,
} from './historyModel'

function group(over: Partial<Group>): Group {
  return {
    key: '8372-25',
    kind: 'internal_id',
    matched_case_id: null,
    matched_case_title: null,
    topic: null,
    count: 10,
    ingested_count: 0,
    first_at: '2025-03-01T10:00:00Z',
    last_at: '2025-06-01T10:00:00Z',
    ...over,
  }
}

describe('splitTopic', () => {
  it('splits parties from matter', () => {
    expect(splitTopic('Hansen ./. Liu - wg. Sorge')).toEqual({
      parties: 'Hansen ./. Liu',
      matter: 'wg. Sorge',
    })
  })
  it('keeps later separators inside the matter', () => {
    expect(splitTopic('Hansen ./. Liu - wg. Scheidung - Kostenfestsetzung').matter).toBe(
      'wg. Scheidung - Kostenfestsetzung',
    )
  })
  it('collapses a doubled separator and an en dash', () => {
    expect(splitTopic('Hansen/Liu - - Trennungsunterhalt')).toEqual({
      parties: 'Hansen/Liu',
      matter: 'Trennungsunterhalt',
    })
    expect(splitTopic('A – B').matter).toBe('B')
  })
  it('returns the whole topic as matter without a separator', () => {
    expect(splitTopic('Hansen ./. Liu')).toEqual({ parties: '', matter: 'Hansen ./. Liu' })
    expect(splitTopic(null)).toEqual({ parties: '', matter: '' })
  })
})

describe('filterGroups', () => {
  const groups = [
    group({
      key: 'a',
      matched_case_id: 'ADV-1',
      matched_case_title: 'Sorgerecht',
      count: 2,
      ingested_count: 2,
    }),
    group({ key: 'b', topic: 'Kindesunterhalt', count: 5, ingested_count: 1 }),
    group({ key: 'c', topic: 'Zugewinn', count: 4, ingested_count: 0 }),
  ]
  const keys = (opts: Parameters<typeof filterGroups>[1]) =>
    filterGroups(groups, opts).map((g) => g.key)

  it('filters by chip', () => {
    expect(keys({ query: '', filter: 'all' })).toEqual(['a', 'b', 'c'])
    expect(keys({ query: '', filter: 'case' })).toEqual(['a'])
    expect(keys({ query: '', filter: 'nocase' })).toEqual(['b', 'c'])
    expect(keys({ query: '', filter: 'pending' })).toEqual(['b', 'c'])
    expect(keys({ query: '', filter: 'done' })).toEqual(['a'])
  })
  it('searches reference, topic, case id and case title', () => {
    expect(keys({ query: 'kindes', filter: 'all' })).toEqual(['b'])
    expect(keys({ query: 'adv-1', filter: 'all' })).toEqual(['a'])
    expect(keys({ query: 'sorge', filter: 'all' })).toEqual(['a'])
    expect(keys({ query: 'zzz', filter: 'all' })).toEqual([])
  })
})

describe('sorting', () => {
  const a = group({
    key: 'a',
    first_at: '2025-01-01T00:00:00Z',
    last_at: '2025-02-01T00:00:00Z',
    count: 5,
    ingested_count: 5,
  })
  const b = group({
    key: 'b',
    first_at: '2024-01-01T00:00:00Z',
    last_at: '2026-01-01T00:00:00Z',
    count: 9,
    ingested_count: 1,
  })
  const c = group({
    key: 'c',
    first_at: '2025-06-01T00:00:00Z',
    last_at: '2025-07-01T00:00:00Z',
    count: 3,
    ingested_count: 0,
  })
  const order = (sort: Parameters<typeof sortGroups>[1]) =>
    sortGroups([a, b, c], sort).map((g) => g.key)

  it('orders oldest history, recent activity and most pending', () => {
    expect(order('oldest')).toEqual(['b', 'a', 'c'])
    expect(order('recent')).toEqual(['b', 'c', 'a'])
    expect(order('pending')).toEqual(['b', 'c', 'a'])
  })
  it('sorts a case bucket by its combined totals', () => {
    const inCase = [
      group({ key: 'x', matched_case_id: 'C1', first_at: '2025-05-01T00:00:00Z' }),
      group({ key: 'y', matched_case_id: 'C1', first_at: '2023-05-01T00:00:00Z' }),
      group({ key: 'z', matched_case_id: 'C2', first_at: '2024-05-01T00:00:00Z' }),
    ]
    const sorted = sortSections(sectionGroups(inCase), 'oldest')
    expect(sorted.cases.map((b) => b.id)).toEqual(['C1', 'C2'])
    expect(sorted.cases[0]?.groups.map((g) => g.key)).toEqual(['y', 'x'])
  })
})

describe('groupTotals', () => {
  it('sums counts and spans the earliest to the latest date', () => {
    const t = groupTotals([
      group({
        count: 2,
        ingested_count: 1,
        first_at: '2025-05-01T00:00:00Z',
        last_at: '2025-06-01T00:00:00Z',
      }),
      group({
        count: 3,
        ingested_count: 3,
        first_at: '2025-01-01T00:00:00Z',
        last_at: '2025-09-01T00:00:00Z',
      }),
    ])
    expect(t).toEqual({
      count: 5,
      ingested: 4,
      first: '2025-01-01T00:00:00Z',
      last: '2025-09-01T00:00:00Z',
    })
  })
})

describe('timeAxis', () => {
  it('runs from 1 Jan of the first year to the end of the last', () => {
    const axis = timeAxis([
      group({ first_at: '2024-07-01T00:00:00Z', last_at: '2026-03-01T00:00:00Z' }),
    ])
    expect(axis.years.map((y) => y.year)).toEqual([2024, 2025, 2026])
    expect(axis.years[0]?.at).toBe(0)
    expect(axis.pos('2024-07-01T00:00:00Z')).toBeGreaterThan(0)
    expect(axis.pos('2026-03-01T00:00:00Z')).toBeLessThan(1)
    expect(axis.pos('2025-06-01T00:00:00Z')).toBeGreaterThan(axis.pos('2024-07-01T00:00:00Z'))
  })
  it('is empty without groups', () => {
    expect(timeAxis([]).years).toEqual([])
  })
})
