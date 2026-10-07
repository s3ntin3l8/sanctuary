import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError, type Schemas, unwrap } from './client'

type S = Schemas
export type CostsOverview = S['CostsOverview']
export type CostRow = S['CostRow']

export function useCostsOverview() {
  return useQuery<CostsOverview, ApiError>({
    queryKey: ['costs'],
    queryFn: () => unwrap(api.GET('/api/v1/costs')),
  })
}

function useCostsInvalidate() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: ['costs'] })
    queryClient.invalidateQueries({ queryKey: ['case'] })
    queryClient.invalidateQueries({ queryKey: ['home'] })
  }
}

export function useCreateCost() {
  const invalidate = useCostsInvalidate()
  return useMutation<CostRow, ApiError, { caseId: string } & S['CostCreate']>({
    mutationFn: ({ caseId, ...body }) =>
      unwrap(
        api.POST('/api/v1/cases/{case_id}/costs', {
          params: { path: { case_id: caseId } },
          body,
        }),
      ),
    onSuccess: invalidate,
  })
}

export type CostAction = 'pay' | 'unpay' | 'reimburse' | 'unreimburse'

export function useLedgerAction() {
  const invalidate = useCostsInvalidate()
  return useMutation<CostRow, ApiError, { costId: number; action: CostAction; amount?: number }>({
    mutationFn: ({ costId, action, amount }) => {
      const params = { params: { path: { cost_id: costId } } }
      switch (action) {
        case 'pay':
          return unwrap(api.POST('/api/v1/costs/{cost_id}/pay', params))
        case 'unpay':
          return unwrap(api.POST('/api/v1/costs/{cost_id}/unpay', params))
        case 'reimburse':
          return unwrap(
            api.POST('/api/v1/costs/{cost_id}/reimburse', {
              ...params,
              body: amount !== undefined ? { amount } : {},
            }),
          )
        case 'unreimburse':
          return unwrap(api.POST('/api/v1/costs/{cost_id}/unreimburse', params))
      }
    },
    onSuccess: invalidate,
  })
}

export function useEditCost() {
  const invalidate = useCostsInvalidate()
  return useMutation<CostRow, ApiError, { costId: number } & S['CostFieldUpdate']>({
    mutationFn: ({ costId, ...body }) =>
      unwrap(api.PATCH('/api/v1/costs/{cost_id}', { params: { path: { cost_id: costId } }, body })),
    onSuccess: invalidate,
  })
}

export function useContact(name: string) {
  return useQuery<S['ContactView'], ApiError>({
    queryKey: ['contact', name],
    queryFn: () => unwrap(api.GET('/api/v1/contacts', { params: { query: { name } } })),
    enabled: name.length > 0,
  })
}
