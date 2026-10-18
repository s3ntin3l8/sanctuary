const eur = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 })

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
