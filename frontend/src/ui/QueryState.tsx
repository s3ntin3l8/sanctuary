import type { ApiError } from '../api/client'

/** Loading / error placeholder for a page whose data has not arrived. */
export function QueryState({ error, pending }: { error: ApiError | null; pending: boolean }) {
  if (error) {
    return (
      <p
        role="alert"
        className="rounded-xl border border-danger/40 bg-danger/10 px-4 py-3 text-[12px] text-danger"
      >
        {error.status === 403 ? 'Only an administrator can open this page.' : error.message}
      </p>
    )
  }
  if (pending) return <p className="px-1 py-3 text-[12px] text-muted">Loading…</p>
  return null
}
