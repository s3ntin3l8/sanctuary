import { useSyncExternalStore } from 'react'

/**
 * Subscribe to layout changes so secondary panels mount in only one place.
 * This client-only Vite SPA uses an unmatched server snapshot; SSR consumers
 * would need an explicit hydration strategy rather than relying on that default.
 */
export function useMediaQuery(query: string) {
  return useSyncExternalStore(
    (notify) => {
      const media = window.matchMedia(query)
      media.addEventListener('change', notify)
      return () => media.removeEventListener('change', notify)
    },
    () => window.matchMedia(query).matches,
    () => false,
  )
}
