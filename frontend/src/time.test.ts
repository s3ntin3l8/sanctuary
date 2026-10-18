import { afterEach, beforeEach, describe, expect, test } from 'vitest'

import {
  civilDay,
  dayNumber,
  dayToApi,
  daysUntil,
  displayTimeZone,
  formatAgo,
  formatDateTime,
  formatDueRelative,
  formatIsoDate,
  formatLongDate,
  formatMonthLabel,
  formatMonthYear,
  formatShortDate,
  formatTime,
  isPast,
  parseApiDate,
  setDisplayTimeZone,
  todayIso,
} from './time'

// The browser is deliberately NOT in the configured zone: dates must follow the setting.
const hostTz = process.env.TZ
beforeEach(() => {
  process.env.TZ = 'America/Los_Angeles'
  setDisplayTimeZone('Pacific/Auckland')
})
afterEach(() => {
  if (hostTz === undefined) delete process.env.TZ
  else process.env.TZ = hostTz
})

describe('parsing', () => {
  test('naive API datetimes are UTC, not browser-local', () => {
    expect(parseApiDate('2026-06-17T10:00:00').toISOString()).toBe('2026-06-17T10:00:00.000Z')
    expect(parseApiDate('2026-06-17T10:00:00.250').toISOString()).toBe('2026-06-17T10:00:00.250Z')
  })

  test('explicit offsets are honoured and a bare date is its UTC midnight', () => {
    expect(parseApiDate('2026-06-17T10:00:00Z').toISOString()).toBe('2026-06-17T10:00:00.000Z')
    expect(parseApiDate('2026-06-17T12:00:00+02:00').toISOString()).toBe('2026-06-17T10:00:00.000Z')
    expect(parseApiDate('2026-06-17').toISOString()).toBe('2026-06-17T00:00:00.000Z')
  })

  test('the configured zone is what the module reports', () => {
    expect(displayTimeZone()).toBe('Pacific/Auckland')
  })
})

describe('configured zone', () => {
  test('an instant lands on the configured calendar day', () => {
    // 22:00 UTC is already the next morning in Auckland (UTC+12 in June).
    expect(civilDay('2026-06-17T22:00:00')).toBe('2026-06-18')
    expect(formatIsoDate('2026-06-17T22:00:00')).toBe('2026-06-18')
    expect(formatShortDate('2026-06-17T22:00:00')).toBe('18 Jun')
    expect(formatLongDate('2026-06-17T22:00:00')).toBe('Thursday, 18 June 2026')
    expect(formatMonthYear('2026-06-30T13:00:00')).toBe('07.26')
    expect(formatMonthLabel('2026-06-30T13:00:00')).toBe('Jul 2026')
  })

  test('clock times are read in the configured zone', () => {
    expect(formatTime('2026-06-17T22:05:00')).toBe('10:05')
    expect(formatDateTime('2026-06-17T22:05:00')).toBe('2026-06-18 10:05')
  })

  test('a different configured zone moves the same instant', () => {
    setDisplayTimeZone('America/Los_Angeles')
    expect(civilDay('2026-06-17T05:00:00')).toBe('2026-06-16')
    setDisplayTimeZone('Europe/Berlin')
    expect(civilDay('2026-06-17T22:30:00')).toBe('2026-06-18')
    setDisplayTimeZone('UTC')
    expect(civilDay('2026-06-17T22:30:00')).toBe('2026-06-17')
  })
})

describe('printed calendar dates', () => {
  test('exactly 00:00:00 is never shifted, in any zone', () => {
    for (const tz of ['Pacific/Auckland', 'America/Los_Angeles', 'UTC', 'Pacific/Kiritimati']) {
      setDisplayTimeZone(tz)
      expect(civilDay('2026-06-18T00:00:00')).toBe('2026-06-18')
      expect(formatShortDate('2026-06-18T00:00:00Z')).toBe('18 Jun')
      expect(formatLongDate('2026-06-18T00:00:00')).toBe('Thursday, 18 June 2026')
      expect(formatMonthYear('2026-06-01T00:00:00')).toBe('06.26')
      expect(formatMonthLabel('2026-06-01T00:00:00')).toBe('Jun 2026')
      expect(formatDateTime('2026-06-18T00:00:00')).toBe('2026-06-18')
    }
  })

  test('a bare date and a midnight datetime are the same day', () => {
    expect(civilDay('2026-06-18')).toBe('2026-06-18')
  })

  test('one second past midnight is an instant again', () => {
    expect(civilDay('2026-06-18T00:00:01')).toBe('2026-06-18')
    setDisplayTimeZone('America/Los_Angeles')
    expect(civilDay('2026-06-18T00:00:01')).toBe('2026-06-17')
  })

  test('date inputs are stored as printed dates', () => {
    expect(dayToApi('2026-06-18')).toBe('2026-06-18T00:00:00.000Z')
    expect(civilDay(dayToApi('2026-06-18'))).toBe('2026-06-18')
  })
})

describe('days', () => {
  const now = new Date('2026-06-14T10:00:00Z') // 22:00 on 14 Jun in Auckland

  test('today is the configured calendar day, not the browser or UTC one', () => {
    expect(todayIso(now)).toBe('2026-06-14')
    expect(todayIso(new Date('2026-06-14T12:30:00Z'))).toBe('2026-06-15')
  })

  test('due dates count calendar days on the reader calendar', () => {
    expect(daysUntil('2026-06-18T00:00:00Z', now)).toBe(4)
    expect(formatDueRelative('2026-06-18T00:00:00Z', now)).toBe('in 4d')
    expect(formatDueRelative('2026-06-14T00:00:00Z', now)).toBe('today')
    expect(formatDueRelative('2026-06-12T00:00:00Z', now)).toBe('2d late')
    // 11:00 UTC is 23:00 in Auckland on the same day...
    expect(formatDueRelative('2026-06-14T11:00:00Z', now)).toBe('today')
    // ...but 12:30 UTC is already tomorrow there.
    expect(formatDueRelative('2026-06-14T12:30:00Z', now)).toBe('in 1d')
  })

  test('overdue flips at the reader midnight, matching the backend classification', () => {
    const before = new Date('2026-06-14T11:59:00Z') // 23:59 in Auckland
    const after = new Date('2026-06-14T12:00:00Z') // 00:00 the next day
    expect(formatDueRelative('2026-06-14T00:00:00Z', before)).toBe('today')
    expect(formatDueRelative('2026-06-14T00:00:00Z', after)).toBe('1d late')
  })

  test('day numbers have no DST in them', () => {
    expect(dayNumber('2026-03-29') - dayNumber('2026-03-28')).toBe(1)
    expect(dayNumber('2026-10-26') - dayNumber('2026-10-25')).toBe(1)
    expect(dayNumber('2026-01-01') - dayNumber('2025-01-01')).toBe(365)
  })

  test('isPast compares instants', () => {
    expect(isPast('2026-06-14T09:59:59', now)).toBe(true)
    expect(isPast('2026-06-14T10:00:01', now)).toBe(false)
  })
})

describe('DST edges', () => {
  test('Berlin spring forward (29 Mar 2026): 23-hour day, days still step by one', () => {
    setDisplayTimeZone('Europe/Berlin')
    expect(civilDay('2026-03-28T22:59:00')).toBe('2026-03-28') // 23:59 CET
    expect(civilDay('2026-03-28T23:00:00')).toBe('2026-03-29') // 00:00 CET
    expect(civilDay('2026-03-29T21:59:00')).toBe('2026-03-29') // 23:59 CEST
    expect(civilDay('2026-03-29T22:00:00')).toBe('2026-03-30') // 00:00 CEST
    expect(daysUntil('2026-03-30T00:00:00', new Date('2026-03-28T23:30:00Z'))).toBe(1)
  })

  test('Berlin fall back (25 Oct 2026): 25-hour day', () => {
    setDisplayTimeZone('Europe/Berlin')
    expect(civilDay('2026-10-24T22:00:00')).toBe('2026-10-25') // 00:00 CEST
    expect(civilDay('2026-10-25T22:59:00')).toBe('2026-10-25') // 23:59 CET
    expect(civilDay('2026-10-25T23:00:00')).toBe('2026-10-26') // 00:00 CET
    expect(formatTime('2026-10-25T01:30:00')).toBe('02:30') // CET, the repeated hour
  })

  test('Auckland spring forward (27 Sep 2026)', () => {
    expect(civilDay('2026-09-26T11:59:00')).toBe('2026-09-26') // 23:59 NZST
    expect(civilDay('2026-09-26T12:00:00')).toBe('2026-09-27') // 00:00 NZST
    expect(civilDay('2026-09-27T10:59:00')).toBe('2026-09-27') // 23:59 NZDT
    expect(civilDay('2026-09-27T11:00:00')).toBe('2026-09-28') // 00:00 NZDT
  })

  test('24 hours ago on a 25-hour day is the same calendar day but still reads 1d', () => {
    setDisplayTimeZone('Europe/Berlin')
    const now = new Date('2026-10-25T22:30:00Z') // 23:30 CET on the 25th
    expect(formatAgo('2026-10-24T22:30:00', now)).toBe('1d') // 00:30 CEST on the 25th
  })
})

describe('formatAgo', () => {
  const now = new Date('2026-06-17T12:00:00Z')

  test('compresses to minutes, hours, days, then the date', () => {
    expect(formatAgo('2026-06-17T11:59:40Z', now)).toBe('now')
    expect(formatAgo('2026-06-17T11:52:00Z', now)).toBe('8m')
    expect(formatAgo('2026-06-17T09:00:00Z', now)).toBe('3h')
    expect(formatAgo('2026-06-15T12:00:00Z', now)).toBe('2d')
  })

  test('a naive stamp is UTC, so it reads the same wherever the browser is', () => {
    expect(formatAgo('2026-06-17T09:00:00', now)).toBe('3h')
  })

  test('older than two weeks falls back to the configured-zone date', () => {
    expect(formatAgo('2026-05-01T12:00:00Z', now)).toBe('02 May')
    expect(formatAgo('2026-05-01T00:00:00Z', now)).toBe('01 May')
  })
})
