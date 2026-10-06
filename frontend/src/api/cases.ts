import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type ApiError, type Schemas, unwrap } from './client'

export type CaseCard = Schemas['CaseCard']

export function useCasesDirectory() {
  return useQuery<Schemas['CasesDirectory'], ApiError>({
    queryKey: ['cases'],
    queryFn: () => unwrap(api.GET('/api/v1/cases')),
  })
}

export function useCreateCase() {
  const queryClient = useQueryClient()
  return useMutation<Schemas['CaseCreated'], ApiError, Schemas['CaseCreate']>({
    mutationFn: (body) => unwrap(api.POST('/api/v1/cases', { body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['cases'] }),
  })
}

export function useCloseDecision() {
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError, { caseId: string; decision: 'confirm' | 'dismiss' }>({
    mutationFn: ({ caseId, decision }) =>
      unwrap(
        decision === 'confirm'
          ? api.POST('/api/v1/cases/{case_id}/confirm-close', {
              params: { path: { case_id: caseId } },
            })
          : api.POST('/api/v1/cases/{case_id}/dismiss-close', {
              params: { path: { case_id: caseId } },
            }),
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cases'] })
      queryClient.invalidateQueries({ queryKey: ['home'] })
    },
  })
}
