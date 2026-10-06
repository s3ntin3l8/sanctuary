import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type ApiError, type Schemas, unwrap } from './client'

export function useHome() {
  return useQuery<Schemas['HomeView'], ApiError>({
    queryKey: ['home'],
    queryFn: () => unwrap(api.GET('/api/v1/home')),
    // Keep the triage pipeline chips live while something is still processing.
    refetchInterval: (query) => {
      const bundles = query.state.data?.triage_bundles ?? []
      const busy = bundles.some((b) => b.pipeline.running + b.pipeline.pending > 0)
      return busy ? 4_000 : false
    },
  })
}

export function useReviewAll() {
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError>({
    mutationFn: () => unwrap(api.POST('/api/v1/home/review-all')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['home'] }),
  })
}
