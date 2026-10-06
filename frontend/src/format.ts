const DAY_MS = 86_400_000

const shortDate = new Intl.DateTimeFormat('en-GB', { day: '2-digit', month: 'short' })
const longDate = new Intl.DateTimeFormat('en-GB', {
  weekday: 'long',
  day: 'numeric',
  month: 'long',
  year: 'numeric',
})
const isoDate = new Intl.DateTimeFormat('sv-SE', { dateStyle: 'short' })
const eur = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 })

/** "18 Jun" */
export function formatShortDate(iso: string | null | undefined) {
  return iso ? shortDate.format(new Date(iso)) : ''
}

/** "Tuesday, 17 June 2026" */
export function formatLongDate(iso: string) {
  return longDate.format(new Date(iso))
}

/** "2026-06-18" */
export function formatIsoDate(iso: string | null | undefined) {
  return iso ? isoDate.format(new Date(iso)) : ''
}

function startOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

/** Calendar days from today until the day of `iso` (negative when past). */
export function daysUntil(iso: string, now = new Date()) {
  return Math.round((startOfDay(new Date(iso)) - startOfDay(now)) / DAY_MS)
}

/** "today" · "in 3d" · "2d late" */
export function formatDueRelative(iso: string, now = new Date()) {
  const days = daysUntil(iso, now)
  if (days === 0) return 'today'
  if (days < 0) return `${-days}d late`
  return `in ${days}d`
}

/** "€18,450" */
export function formatEur(amount: number) {
  return `€${eur.format(amount)}`
}

/** "€195.5k" for KPI tiles. */
export function formatEurCompact(amount: number) {
  if (amount >= 1000) return `€${(amount / 1000).toFixed(1).replace(/\.0$/, '')}k`
  return formatEur(amount)
}

export function pluralize(count: number, noun: string) {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

/** "KV" from "Katharina Vogt", "K" from an email. */
export function initials(name: string) {
  const parts = name.split(/[\s@._-]+/).filter(Boolean)
  return parts
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase() ?? '')
    .join('')
}
