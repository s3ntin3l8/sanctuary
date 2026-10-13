import { useSyncExternalStore } from 'react'

/** Subscribe to layout changes so secondary panels mount in only one place. */
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
