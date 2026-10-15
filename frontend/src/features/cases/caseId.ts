/** Mirrors the server's `normalize_case_id`, so the field shows what will be stored. */
export function normalizeCaseId(raw: string): string {
  return raw.replaceAll('/', '-').trim().toUpperCase()
}
