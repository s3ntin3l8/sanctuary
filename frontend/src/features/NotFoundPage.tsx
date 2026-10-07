import { useEffect } from 'react'
import { Link, useLocation } from 'react-router'

import { Icon } from '../ui/Icon'

/** Rendered for any path the router does not own; the server already answered 404. */
export function NotFoundPage() {
  const { pathname } = useLocation()
  useEffect(() => {
    document.title = 'Not found | The Sanctuary'
  }, [])
  return (
    <main className="mx-auto flex max-w-[520px] flex-col items-center gap-3 p-10 text-center text-[12px]">
      <Icon name="explore_off" size={28} className="text-muted" />
      <h1 className="font-display text-[18px] font-extrabold">Nothing here</h1>
      <p className="text-muted">
        <code className="font-mono text-[11px]">{pathname}</code> is not a page in the Sanctuary.
      </p>
      <Link to="/" className="text-tealink hover:underline">
        Back to Home
      </Link>
    </main>
  )
}
