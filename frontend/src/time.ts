/**
 * Every date the SPA reads, compares or prints goes through here.
 *
 * The API sends naive ISO datetimes that are UTC (no offset), which `new Date()`
 * would read as browser-local. The reader's calendar is the workspace's configured
 * IANA timezone (`ShellView.timezone`), not the browser's, so the SPA agrees with
 * the AI, MCP and review text on which day something happened
 * (`app/services/timezone_service.py`).
 *
 * A value at exactly 00:00:00 is a printed calendar date (a letter's date), not an
 * instant: it is never shifted into the configured zone, matching
 * `timezone_service.local_date_str`.
 */

import { useSyncExternalStore } from 'react'

const DAY_MS = 86_400_000

let zone: string | null = null
const listeners = new Set<() => void>()

/**
 * Set from the signed-in shell payload (null = not known yet). Nothing guesses a zone:
 * reading a date before it is set throws, and the shell mounts no page until it is.
 */
export function setDisplayTimeZone(tz: string | null) {
  if (tz === zone) return
  zone = tz
  listeners.forEach((notify) => notify())
}

function subscribe(notify: () => void) {
  listeners.add(notify)
  return () => listeners.delete(notify)
}

/** The zone dates are read in, re-rendering the caller when it changes. */
export function useDisplayTimeZone() {
  return useSyncExternalStore(subscribe, () => zone)
}

function activeZone() {
  if (zone === null) throw new Error('The display timezone has not been set')
  return zone
}

export function displayTimeZone() {
  return activeZone()
}

const OFFSET = /(?:Z|[+-]\d{2}:?\d{2})$/i
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

/** An API datetime as an instant: no offset means UTC, a bare date is its midnight. */
export function parseApiDate(iso: string): Date {
  if (DATE_ONLY.test(iso)) return new Date(`${iso}T00:00:00Z`)
  return new Date(OFFSET.test(iso) ? iso : `${iso}Z`)
}

/** A printed calendar date: exactly 00:00:00.000 UTC. */
function isPrinted(d: Date) {
  return d.getUTCHours() + d.getUTCMinutes() + d.getUTCSeconds() + d.getUTCMilliseconds() === 0
}

const formatters = new Map<string, Intl.DateTimeFormat>()

function formatter(locale: string, timeZone: string, options: Intl.DateTimeFormatOptions) {
  const key = `${locale}|${timeZone}|${JSON.stringify(options)}`
  let f = formatters.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat(locale, { ...options, timeZone })
    formatters.set(key, f)
  }
  return f
}

/** `YYYY-MM-DD` of an instant on the configured calendar (no printed-date rule). */
function instantDay(d: Date) {
  const parts = formatter('en-GB', activeZone(), {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(d)
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}`
}

/** The calendar day of an API value for the reader, `YYYY-MM-DD`. */
export function civilDay(iso: string) {
  const d = parseApiDate(iso)
  return isPrinted(d) ? d.toISOString().slice(0, 10) : instantDay(d)
}

/** Today on the configured calendar, `YYYY-MM-DD`. */
export function todayIso(now = new Date()) {
  return instantDay(now)
}

/** Whole days since 1970-01-01 of a `YYYY-MM-DD` day, so day math has no DST in it. */
export function dayNumber(day: string) {
  return Math.round(Date.parse(`${day}T00:00:00Z`) / DAY_MS)
}

/** Calendar days from today until the day of `iso` (negative when past). */
export function daysUntil(iso: string, now = new Date()) {
  return dayNumber(civilDay(iso)) - dayNumber(todayIso(now))
}

/** Whether an instant (an expiry, a deadline stamp) has passed. */
export function isPast(iso: string, now = new Date()) {
  return parseApiDate(iso).getTime() <= now.getTime()
}

/** Milliseconds since the epoch of an API instant. */
export function toMillis(iso: string) {
  return parseApiDate(iso).getTime()
}

/** An API value formatted on the reader's calendar (printed dates keep their day). */
function format(iso: string, options: Intl.DateTimeFormatOptions) {
  const d = parseApiDate(iso)
  return formatter('en-GB', isPrinted(d) ? 'UTC' : activeZone(), options).format(d)
}

/** The value a `<input type="date">` day is stored as: a printed date. */
export function dayToApi(day: string) {
  return new Date(`${day}T00:00:00Z`).toISOString()
}

/** "18 Jun '26" */
export function formatShortDate(iso: string | null | undefined) {
  if (!iso) return ''
  const d = parseApiDate(iso)
  const zone = isPrinted(d) ? 'UTC' : activeZone()
  const parts = formatter('en-GB', zone, {
    day: '2-digit',
    month: 'short',
    year: '2-digit',
  }).formatToParts(d)
  const day = parts.find((p) => p.type === 'day')?.value ?? ''
  const month = parts.find((p) => p.type === 'month')?.value ?? ''
  const year = parts.find((p) => p.type === 'year')?.value ?? ''
  return `${day} ${month} '${year}`
}

/** "Tuesday, 17 June 2026" */
export function formatLongDate(iso: string) {
  return format(iso, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
}

/** "2026-06-18" */
export function formatIsoDate(iso: string | null | undefined) {
  return iso ? civilDay(iso) : ''
}

/** "10.26" */
export function formatMonthYear(iso: string | null | undefined) {
  if (!iso) return ''
  const day = civilDay(iso)
  return `${day.slice(5, 7)}.${day.slice(2, 4)}`
}

/** "Oct 2026" */
export function formatMonthLabel(iso: string) {
  return format(iso, { month: 'short', year: 'numeric' })
}

/** "14:05" — the clock time of an instant in the configured zone. */
export function formatTime(iso: string) {
  return formatter('en-GB', activeZone(), {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(parseApiDate(iso))
}

/** "2026-06-18 14:05"; a printed date has no time of day. */
export function formatDateTime(iso: string) {
  return isPrinted(parseApiDate(iso)) ? civilDay(iso) : `${civilDay(iso)} ${formatTime(iso)}`
}

/** "today" · "in 3d" · "2d late" */
export function formatDueRelative(iso: string, now = new Date()) {
  const days = daysUntil(iso, now)
  if (days === 0) return 'today'
  if (days < 0) return `${-days}d late`
  return `in ${days}d`
}

/** "8m" · "3h" · "2d" · "18 Jun" — how long ago `iso` was, prototype-style. */
export function formatAgo(iso: string, now = new Date()) {
  const then = parseApiDate(iso)
  const minutes = Math.round((now.getTime() - then.getTime()) / 60_000)
  if (minutes < 1) return 'now'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h`
  // From a day on, count calendar days on the reader's calendar.
  const days = Math.max(1, dayNumber(instantDay(now)) - dayNumber(instantDay(then)))
  if (days < 14) return `${days}d`
  return formatShortDate(iso)
}
