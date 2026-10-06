import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError, type Schemas, unwrap } from './client'

type S = Schemas
export type Reader = S['DocumentReader']
export type Pin = S['PinView']

const docPath = (docId: number) => ({ params: { path: { doc_id: docId } } })
export const readerKey = (docId: number) => ['document', docId, 'reader'] as const

/** The full-screen HUD: review data plus the rendered body, pins and navigation. */
export function useDocumentReader(docId: number) {
  return useQuery<Reader, ApiError>({
    queryKey: readerKey(docId),
    queryFn: () => unwrap(api.GET('/api/v1/documents/{doc_id}/reader', docPath(docId))),
    refetchInterval: (query) =>
      ['pending', 'running', 'partial'].includes(query.state.data?.pipeline.state ?? '')
        ? 4_000
        : false,
  })
}

export function originalUrl(docId: number) {
  return `/api/v1/documents/${docId}/original`
}

function usePinsPatch(docId: number) {
  const queryClient = useQueryClient()
  return (update: (pins: Pin[]) => Pin[]) =>
    queryClient.setQueryData<Reader>(readerKey(docId), (prev) => {
      if (!prev) return prev
      const pins = update(prev.pins)
      const counts = new Map<string, number>()
      for (const p of pins) counts.set(p.passage_id, (counts.get(p.passage_id) ?? 0) + 1)
      return {
        ...prev,
        pins,
        key_passages: prev.key_passages.map((p) => ({ ...p, pin_count: counts.get(p.id) ?? 0 })),
      }
    })
}

export function useCreatePin(docId: number) {
  const patch = usePinsPatch(docId)
  return useMutation<Pin, ApiError, S['PinCreate']>({
    mutationFn: (body) =>
      unwrap(api.POST('/api/v1/documents/{doc_id}/pins', { ...docPath(docId), body })),
    onSuccess: (pin) => patch((pins) => [...pins, pin]),
  })
}

export function useUpdatePin(docId: number) {
  const patch = usePinsPatch(docId)
  return useMutation<Pin, ApiError, { pinId: number; note: string | null }>({
    mutationFn: ({ pinId, note }) =>
      unwrap(
        api.PATCH('/api/v1/pins/{pin_id}', { params: { path: { pin_id: pinId } }, body: { note } }),
      ),
    onSuccess: (pin) => patch((pins) => pins.map((p) => (p.id === pin.id ? pin : p))),
  })
}

export function useDeletePin(docId: number) {
  const patch = usePinsPatch(docId)
  return useMutation<unknown, ApiError, number>({
    mutationFn: (pinId) =>
      unwrap(api.DELETE('/api/v1/pins/{pin_id}', { params: { path: { pin_id: pinId } } })),
    onSuccess: (_, pinId) => patch((pins) => pins.filter((p) => p.id !== pinId)),
  })
}
