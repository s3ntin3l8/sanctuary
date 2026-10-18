import { expect, test } from 'vitest'

import { formatEur, formatEurCompact, initials, pluralize } from './format'

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

test('pluralize adds an s except for one', () => {
  expect(pluralize(1, 'doc')).toBe('1 doc')
  expect(pluralize(2, 'doc')).toBe('2 docs')
})
