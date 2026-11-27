import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError, type Schemas, unwrap } from './client'

export type ReviewFinding = Schemas['ReviewFindingView']
export type VerifierStatus = Schemas['VerifierStatus']

const KEY = 'review-findings'

/** Second opinions on one document's AI output, newest first. */
export function useDocumentFindings(docId: number) {
  return useQuery<ReviewFinding[], ApiError>({
    queryKey: [KEY, 'document', docId],
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/documents/{doc_id}/review-findings', {
          params: { path: { doc_id: docId } },
        }),
      ),
  })
}

/** Second opinions on a case: its brief and the documents filed in it. */
export function useCaseFindings(caseId: string) {
  return useQuery<ReviewFinding[], ApiError>({
    queryKey: [KEY, 'case', caseId],
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/cases/{case_id}/review-findings', {
          params: { path: { case_id: caseId } },
        }),
      ),
  })
}

/**
 * Confirm (apply the proposed value) or dismiss a pending finding. Confirming can change the
 * document, the case, the triage bundle and its review reasons, so all of them refresh.
 */
export function useFindingDecision() {
  const queryClient = useQueryClient()
  return useMutation<ReviewFinding, ApiError, { id: number; decision: 'confirm' | 'dismiss' }>({
    mutationFn: ({ id, decision }) => {
      const p = { params: { path: { finding_id: id } } }
      return unwrap(
        decision === 'confirm'
          ? api.POST('/api/v1/review-findings/{finding_id}/confirm', p)
          : api.POST('/api/v1/review-findings/{finding_id}/dismiss', p),
      )
    },
    onSettled: () => {
      for (const key of [KEY, 'document', 'triage', 'case', 'shell']) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
    },
  })
}

/** Whether the verifier may check this document or case (switched on, case in scope). */
export function useVerifierStatus(target: { docId: number } | { caseId: string }) {
  return useQuery<VerifierStatus, ApiError>({
    queryKey: ['verifier', target],
    queryFn: () =>
      unwrap(
        'docId' in target
          ? api.GET('/api/v1/documents/{doc_id}/verifier', {
              params: { path: { doc_id: target.docId } },
            })
          : api.GET('/api/v1/cases/{case_id}/verifier', {
              params: { path: { case_id: target.caseId } },
            }),
      ),
    staleTime: 60_000,
  })
}

/** When to look again for the findings of a queued verifier check (it answers in the background). */
export const VERIFY_REFRESH_MS = [10_000, 30_000, 60_000, 120_000]

/** Queue the verifier's check; its findings arrive in "Second opinion" when it is done. */
export function useVerify() {
  const queryClient = useQueryClient()
  return useMutation<Schemas['VerifyQueued'], ApiError, { docId: number } | { caseId: string }>({
    onSuccess: () => {
      for (const delay of VERIFY_REFRESH_MS) {
        setTimeout(() => queryClient.invalidateQueries({ queryKey: [KEY] }), delay)
      }
    },
    mutationFn: (target) =>
      unwrap(
        'docId' in target
          ? api.POST('/api/v1/documents/{doc_id}/verify', {
              params: { path: { doc_id: target.docId } },
            })
          : api.POST('/api/v1/cases/{case_id}/verify', {
              params: { path: { case_id: target.caseId } },
            }),
      ),
  })
}
