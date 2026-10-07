import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type ApiError, type Schemas, unwrap } from './client'

type S = Schemas

const POLL_MS = 3_000

export const useGmailIndexStatus = () => {
  const queryClient = useQueryClient()
  return useQuery<S['GmailIndexStatus'], ApiError>({
    queryKey: ['gmail', 'index'],
    queryFn: async () => {
      const status = await unwrap(api.GET('/api/v1/gmail/index/status'))
      const previous = queryClient.getQueryData<S['GmailIndexStatus']>(['gmail', 'index'])
      // The index just finished: the groups and message lists changed under us.
      if (previous?.running && !status.running) {
        queryClient.invalidateQueries({ queryKey: ['gmail', 'groups'] })
        queryClient.invalidateQueries({ queryKey: ['gmail', 'messages'] })
      }
      return status
    },
    refetchInterval: (query) => (query.state.data?.running ? POLL_MS : false),
  })
}

export const useGmailGroups = () =>
  useQuery<S['GmailGroupList'], ApiError>({
    queryKey: ['gmail', 'groups'],
    queryFn: () => unwrap(api.GET('/api/v1/gmail/groups')),
  })

/** Oldest-first message pages of one group ("unreferenced" for mail without a reference). */
export const useGmailMessages = (group: string, enabled: boolean) =>
  useInfiniteQuery<
    S['GmailMessagePage'],
    ApiError,
    { pages: S['GmailMessagePage'][] },
    string[],
    string
  >({
    queryKey: ['gmail', 'messages', group],
    enabled,
    initialPageParam: '',
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET('/api/v1/gmail/messages', {
          params: { query: { group, cursor: pageParam || undefined, limit: 100 } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })

export function useRefreshGmailIndex() {
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError>({
    mutationFn: () => unwrap(api.POST('/api/v1/gmail/index')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['gmail', 'index'] }),
  })
}

export function useGmailImportStatus() {
  const queryClient = useQueryClient()
  return useQuery<S['GmailImportStatus'], ApiError>({
    queryKey: ['gmail', 'import'],
    queryFn: async () => {
      const status = await unwrap(api.GET('/api/v1/gmail/import/status'))
      const previous = queryClient.getQueryData<S['GmailImportStatus']>(['gmail', 'import'])
      // Imported-counts move while a run is active, and once more when it ends.
      if (status.active || previous?.active) {
        queryClient.invalidateQueries({ queryKey: ['gmail', 'groups'] })
        queryClient.invalidateQueries({ queryKey: ['gmail', 'messages'] })
      }
      return status
    },
    refetchInterval: (query) => (query.state.data?.active ? POLL_MS : false),
  })
}

export function useStartGmailImport() {
  const queryClient = useQueryClient()
  return useMutation<S['GmailImportQueued'], ApiError, S['GmailImportRequest']>({
    mutationFn: (body) => unwrap(api.POST('/api/v1/gmail/import', { body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['gmail', 'import'] }),
  })
}

export function useCancelGmailImport() {
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError>({
    mutationFn: () => unwrap(api.DELETE('/api/v1/gmail/import')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['gmail', 'import'] }),
  })
}
