import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError, type Schemas, unwrap } from './client'

type S = Schemas
export type CaseDetail = S['CaseDetail']
export type GraphView = S['GraphView']
export type TimelineView = S['TimelineView']
export type TruthMap = S['TruthMapView']
export type ClaimView = S['ClaimView']
export type Financials = S['FinancialsView']
export type Sharing = S['SharingView']
export type SignificanceFilter = GraphView['filter']
export type TruthMapFilter = TruthMap['filter']

const casePath = (caseId: string) => ({ params: { path: { case_id: caseId } } })
export const caseKey = (caseId: string) => ['case', caseId] as const

export function useCaseDetail(caseId: string, proceeding: number | null) {
  return useQuery<CaseDetail, ApiError>({
    queryKey: [...caseKey(caseId), 'detail', proceeding],
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/cases/{case_id}', {
          params: { path: { case_id: caseId }, query: { proceeding } },
        }),
      ),
    refetchInterval: (q) => (q.state.data?.brief.status === 'processing' ? 4_000 : false),
  })
}

export function useCaseGraph(
  caseId: string,
  proceeding: number | null,
  filter: SignificanceFilter,
) {
  return useQuery<GraphView, ApiError>({
    queryKey: [...caseKey(caseId), 'graph', proceeding, filter],
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/cases/{case_id}/graph', {
          params: { path: { case_id: caseId }, query: { proceeding: proceeding ?? 0, filter } },
        }),
      ),
    enabled: proceeding !== null,
  })
}

export function useCaseTimeline(caseId: string, enabled: boolean) {
  return useQuery<TimelineView, ApiError>({
    queryKey: [...caseKey(caseId), 'timeline'],
    queryFn: () => unwrap(api.GET('/api/v1/cases/{case_id}/timeline', casePath(caseId))),
    enabled,
  })
}

export function useTruthMap(caseId: string, filter: TruthMapFilter, enabled: boolean) {
  return useQuery<TruthMap, ApiError>({
    queryKey: [...caseKey(caseId), 'truthmap', filter],
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/cases/{case_id}/truthmap', {
          params: { path: { case_id: caseId }, query: { filter } },
        }),
      ),
    enabled,
    refetchInterval: (q) => (q.state.data?.dedup_job?.status === 'running' ? 3_000 : false),
  })
}

export function useFinancials(caseId: string, enabled: boolean) {
  return useQuery<Financials, ApiError>({
    queryKey: [...caseKey(caseId), 'financials'],
    queryFn: () => unwrap(api.GET('/api/v1/cases/{case_id}/financials', casePath(caseId))),
    enabled,
  })
}

export function useSharing(caseId: string, enabled: boolean) {
  return useQuery<Sharing, ApiError>({
    queryKey: [...caseKey(caseId), 'sharing'],
    queryFn: () => unwrap(api.GET('/api/v1/cases/{case_id}/shares', casePath(caseId))),
    enabled,
  })
}

/** Invalidate every query of this case (detail, tabs) after a mutation. */
function useCaseInvalidate(caseId: string) {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: caseKey(caseId) })
    queryClient.invalidateQueries({ queryKey: ['cases'] })
    queryClient.invalidateQueries({ queryKey: ['home'] })
  }
}

export function useUpdateCase(caseId: string) {
  const invalidate = useCaseInvalidate(caseId)
  return useMutation<CaseDetail, ApiError, S['CaseUpdate']>({
    mutationFn: (body) =>
      unwrap(api.PATCH('/api/v1/cases/{case_id}', { ...casePath(caseId), body })),
    onSuccess: invalidate,
  })
}

export function usePurgeCase(caseId: string) {
  const invalidate = useCaseInvalidate(caseId)
  return useMutation<unknown, ApiError, string>({
    mutationFn: (confirm) =>
      unwrap(api.POST('/api/v1/cases/{case_id}/purge', { ...casePath(caseId), body: { confirm } })),
    onSuccess: invalidate,
  })
}

export function useOpposingParties(caseId: string) {
  const invalidate = useCaseInvalidate(caseId)
  return useMutation<S['PartyView'][], ApiError, string[]>({
    mutationFn: (opposing_parties) =>
      unwrap(
        api.PUT('/api/v1/cases/{case_id}/opposing-parties', {
          ...casePath(caseId),
          body: { opposing_parties },
        }),
      ),
    onSuccess: invalidate,
  })
}

export function useReenrich(caseId: string) {
  return useMutation<S['ReenrichResult'], ApiError>({
    mutationFn: () => unwrap(api.POST('/api/v1/cases/{case_id}/reenrich', casePath(caseId))),
  })
}

export function useRefreshBrief(caseId: string) {
  const invalidate = useCaseInvalidate(caseId)
  return useMutation<S['BriefView'], ApiError>({
    mutationFn: () => unwrap(api.POST('/api/v1/cases/{case_id}/brief/refresh', casePath(caseId))),
    onSuccess: invalidate,
  })
}

export function useSetActiveProceeding(caseId: string) {
  const invalidate = useCaseInvalidate(caseId)
  return useMutation<unknown, ApiError, number>({
    mutationFn: (proceeding_id) =>
      unwrap(
        api.PUT('/api/v1/cases/{case_id}/active-proceeding', {
          ...casePath(caseId),
          body: { proceeding_id },
        }),
      ),
    onSuccess: invalidate,
  })
}

export function useUpdateProceeding(caseId: string) {
  const invalidate = useCaseInvalidate(caseId)
  return useMutation<S['ProceedingView'], ApiError, { id: number } & S['ProceedingUpdate']>({
    mutationFn: ({ id, ...body }) =>
      unwrap(
        api.PATCH('/api/v1/proceedings/{proceeding_id}', {
          params: { path: { proceeding_id: id } },
          body,
        }),
      ),
    onSuccess: invalidate,
  })
}

export function useDeleteProceeding(caseId: string) {
  const invalidate = useCaseInvalidate(caseId)
  return useMutation<unknown, ApiError, number>({
    mutationFn: (id) =>
      unwrap(
        api.DELETE('/api/v1/proceedings/{proceeding_id}', {
          params: { path: { proceeding_id: id } },
        }),
      ),
    onSuccess: invalidate,
  })
}

export function useCaseActionStatus(caseId: string) {
  const invalidate = useCaseInvalidate(caseId)
  return useMutation<
    S['CaseActionItem'],
    ApiError,
    { itemId: number; status: S['ActionStatusUpdate']['status'] }
  >({
    mutationFn: ({ itemId, status }) =>
      unwrap(
        api.PATCH('/api/v1/action-items/{item_id}', {
          params: { path: { item_id: itemId } },
          body: { status },
        }),
      ),
    onSuccess: invalidate,
  })
}

// --- Claims ------------------------------------------------------------------

function useTruthMapInvalidate(caseId: string) {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: [...caseKey(caseId), 'truthmap'] })
    queryClient.invalidateQueries({ queryKey: [...caseKey(caseId), 'detail'] })
  }
}

export function useClaimStatus(caseId: string) {
  const invalidate = useTruthMapInvalidate(caseId)
  return useMutation<ClaimView, ApiError, { claimId: number; status: S['ClaimStatus'] }>({
    mutationFn: ({ claimId, status }) =>
      unwrap(
        api.PUT('/api/v1/claims/{claim_id}/status', {
          params: { path: { claim_id: claimId } },
          body: { status },
        }),
      ),
    onSuccess: invalidate,
  })
}

export function useClaimPrecedent(caseId: string) {
  const invalidate = useTruthMapInvalidate(caseId)
  return useMutation<ClaimView, ApiError, number>({
    mutationFn: (claimId) =>
      unwrap(
        api.POST('/api/v1/claims/{claim_id}/precedent', {
          params: { path: { claim_id: claimId } },
        }),
      ),
    onSuccess: invalidate,
  })
}

export function useDismissClaim(caseId: string) {
  const invalidate = useTruthMapInvalidate(caseId)
  return useMutation<unknown, ApiError, number>({
    mutationFn: (claimId) =>
      unwrap(api.DELETE('/api/v1/claims/{claim_id}', { params: { path: { claim_id: claimId } } })),
    onSuccess: invalidate,
  })
}

export function useProposalDecision(caseId: string) {
  const invalidate = useTruthMapInvalidate(caseId)
  return useMutation<
    unknown,
    ApiError,
    { kind: 'merge' | 'evidence'; proposalId: number; decision: 'confirm' | 'dismiss' }
  >({
    mutationFn: ({ kind, proposalId, decision }) => {
      const params = { params: { path: { proposal_id: proposalId } } }
      if (kind === 'merge') {
        return unwrap(
          decision === 'confirm'
            ? api.POST('/api/v1/claims/proposals/merge/{proposal_id}/confirm', params)
            : api.POST('/api/v1/claims/proposals/merge/{proposal_id}/dismiss', params),
        )
      }
      return unwrap(
        decision === 'confirm'
          ? api.POST('/api/v1/claims/proposals/evidence/{proposal_id}/confirm', params)
          : api.POST('/api/v1/claims/proposals/evidence/{proposal_id}/dismiss', params),
      )
    },
    onSuccess: invalidate,
  })
}

export function useBatchMerge(caseId: string) {
  const invalidate = useTruthMapInvalidate(caseId)
  return useMutation<S['ProposalBatchResult'], ApiError, 'confirm' | 'dismiss'>({
    mutationFn: (action) =>
      unwrap(
        api.POST('/api/v1/cases/{case_id}/claims/proposals/merge', {
          ...casePath(caseId),
          body: { action },
        }),
      ),
    onSuccess: invalidate,
  })
}

export function useFindDuplicates(caseId: string) {
  const invalidate = useTruthMapInvalidate(caseId)
  return useMutation<S['DedupJob'], ApiError>({
    mutationFn: () =>
      unwrap(api.POST('/api/v1/cases/{case_id}/claims/find-duplicates', casePath(caseId))),
    onSuccess: invalidate,
  })
}

// --- Costs -------------------------------------------------------------------

function useFinancialsInvalidate(caseId: string) {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: [...caseKey(caseId), 'financials'] })
    queryClient.invalidateQueries({ queryKey: [...caseKey(caseId), 'detail'] })
  }
}

export type CostAction = 'pay' | 'unpay' | 'reimburse' | 'unreimburse'

export function useCostAction(caseId: string) {
  const invalidate = useFinancialsInvalidate(caseId)
  return useMutation<S['CostRow'], ApiError, { costId: number; action: CostAction }>({
    mutationFn: ({ costId, action }) => {
      const params = { params: { path: { cost_id: costId } } }
      switch (action) {
        case 'pay':
          return unwrap(api.POST('/api/v1/costs/{cost_id}/pay', params))
        case 'unpay':
          return unwrap(api.POST('/api/v1/costs/{cost_id}/unpay', params))
        case 'reimburse':
          return unwrap(api.POST('/api/v1/costs/{cost_id}/reimburse', params))
        case 'unreimburse':
          return unwrap(api.POST('/api/v1/costs/{cost_id}/unreimburse', params))
      }
    },
    onSuccess: invalidate,
  })
}

export function useUpdateCost(caseId: string) {
  const invalidate = useFinancialsInvalidate(caseId)
  return useMutation<S['CostRow'], ApiError, { costId: number } & S['CostFieldUpdate']>({
    mutationFn: ({ costId, ...body }) =>
      unwrap(api.PATCH('/api/v1/costs/{cost_id}', { params: { path: { cost_id: costId } }, body })),
    onSuccess: invalidate,
  })
}

export function useSignalRole(caseId: string) {
  const invalidate = useFinancialsInvalidate(caseId)
  return useMutation<
    unknown,
    ApiError,
    { signalId: number; role: S['ClientRoleUpdate']['role'] | 'auto' }
  >({
    mutationFn: ({ signalId, role }) => {
      const params = { params: { path: { signal_id: signalId } } }
      if (role === 'auto') {
        return unwrap(api.POST('/api/v1/cost-signals/{signal_id}/auto-detect-role', params))
      }
      return unwrap(
        api.PUT('/api/v1/cost-signals/{signal_id}/client-role', { ...params, body: { role } }),
      )
    },
    onSuccess: invalidate,
  })
}

// --- Sharing -----------------------------------------------------------------

export function useAddShare(caseId: string) {
  const queryClient = useQueryClient()
  return useMutation<Sharing, ApiError, S['ShareCreate']>({
    mutationFn: (body) =>
      unwrap(api.POST('/api/v1/cases/{case_id}/shares', { ...casePath(caseId), body })),
    onSuccess: (view) => queryClient.setQueryData([...caseKey(caseId), 'sharing'], view),
  })
}

export function useRemoveShare(caseId: string) {
  const queryClient = useQueryClient()
  return useMutation<Sharing, ApiError, number>({
    mutationFn: (userId) =>
      unwrap(
        api.DELETE('/api/v1/cases/{case_id}/shares/{user_id}', {
          params: { path: { case_id: caseId, user_id: userId } },
        }),
      ),
    onSuccess: (view) => queryClient.setQueryData([...caseKey(caseId), 'sharing'], view),
  })
}
