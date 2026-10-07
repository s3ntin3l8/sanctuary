import { expect, test } from 'vitest'

import {
  daysUntil,
  formatAgo,
  formatDueRelative,
  formatEur,
  formatEurCompact,
  initials,
} from './format'

const now = new Date('2026-06-14T10:00:00Z')

test('due dates are relative to now in whole days', () => {
  expect(daysUntil('2026-06-18T00:00:00Z', now)).toBe(4)
  expect(formatDueRelative('2026-06-18T00:00:00Z', now)).toBe('in 4d')
  expect(formatDueRelative('2026-06-14T12:00:00Z', now)).toBe('today')
  expect(formatDueRelative('2026-06-12T00:00:00Z', now)).toBe('2d late')
})

test('money is euro-prefixed and compacted for tiles', () => {
  expect(formatEur(18450)).toBe('€18,450')
  expect(formatEurCompact(195500)).toBe('€195.5k')
  expect(formatEurCompact(64000)).toBe('€64k')
  expect(formatEurCompact(640)).toBe('€640')
})

test('initials come from names or emails', () => {
  expect(initials('Katharina Vogt')).toBe('KV')
  expect(initials('k.vogt@ra-vogt.de')).toBe('KV')
})

test('formatAgo compresses to minutes, hours, days, then the date', () => {
  const now = new Date('2026-06-17T12:00:00Z')
  expect(formatAgo('2026-06-17T11:59:40Z', now)).toBe('now')
  expect(formatAgo('2026-06-17T11:52:00Z', now)).toBe('8m')
  expect(formatAgo('2026-06-17T09:00:00Z', now)).toBe('3h')
  expect(formatAgo('2026-06-15T12:00:00Z', now)).toBe('2d')
  expect(formatAgo('2026-05-01T12:00:00Z', now)).toBe('01 May')
})
