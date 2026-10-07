import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type ApiError, type Schemas, unwrap } from './client'

export function useShell() {
  return useQuery<Schemas['ShellView'], ApiError>({
    queryKey: ['shell'],
    queryFn: () => unwrap(api.GET('/api/v1/shell')),
    staleTime: 30_000,
  })
}

export function useLogout() {
  return useMutation<unknown, ApiError>({
    mutationFn: () => unwrap(api.POST('/api/v1/auth/logout')),
  })
}

export function useWorkerQueue() {
  return useQuery<Schemas['QueueView'], ApiError>({
    queryKey: ['worker-queue'],
    queryFn: () => unwrap(api.GET('/api/v1/worker-queue')),
    refetchInterval: 5_000,
  })
}

export function useRetryFailed() {
  const queryClient = useQueryClient()
  return useMutation<Schemas['QueueView'], ApiError>({
    mutationFn: () => unwrap(api.POST('/api/v1/worker-queue/retry-failed')),
    onSuccess: (view) => queryClient.setQueryData(['worker-queue'], view),
  })
}

export function useSearch(q: string, limit = 30) {
  return useQuery<Schemas['SearchResults'], ApiError>({
    queryKey: ['search', q, limit],
    queryFn: () => unwrap(api.GET('/api/v1/search', { params: { query: { q, limit } } })),
    enabled: q.trim().length >= 2,
    staleTime: 60_000,
    placeholderData: (previous) => previous,
  })
}
