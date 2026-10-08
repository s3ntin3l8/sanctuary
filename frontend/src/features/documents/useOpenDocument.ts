import { useCallback } from 'react'
import { useLocation, useNavigate } from 'react-router'

/** Router state the HUD reads to send "back" to the view that opened it. */
export type HudOrigin = { from?: string }

/** Open the document HUD, remembering the current view as the way back. */
export function useOpenDocument() {
  const navigate = useNavigate()
  const { pathname, search } = useLocation()
  return useCallback(
    (docId: number) => navigate(`/document/${docId}`, { state: { from: pathname + search } }),
    [navigate, pathname, search],
  )
}
