import { type QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError, type Schemas, unwrap } from './client'

type S = Schemas
export type TriageBundle = S['TriageBundle']

export type TriageFilters = {
  sort: 'received' | 'docs' | 'status'
  dir: 'asc' | 'desc'
  case_id: string[]
  proceeding_id: string[]
  pipeline_filter: string[]
}

export const defaultFilters: TriageFilters = {
  sort: 'received',
  dir: 'desc',
  case_id: [],
  proceeding_id: [],
  pipeline_filter: [],
}

const KEY = ['triage'] as const

/**
 * A review decision moves the bundle row, the checklist and the case spine's
 * "to review" marker; refresh all three. `documents` also covers the peer of a
 * relationship and every pane showing a case that just changed.
 */
function refreshReviewState(queryClient: QueryClient, { documents = false } = {}) {
  queryClient.invalidateQueries({ queryKey: KEY })
  queryClient.invalidateQueries({ queryKey: ['case'] })
  if (documents) queryClient.invalidateQueries({ queryKey: ['document'] })
}

export function useTriage(filters: TriageFilters) {
  return useQuery<S['TriageView'], ApiError>({
    queryKey: [...KEY, filters],
    queryFn: () => unwrap(api.GET('/api/v1/triage', { params: { query: filters } })),
    // Keep pipeline chips live while anything is still being processed.
    refetchInterval: (query) =>
      query.state.data?.bundles.some((b) => b.status === 'processing') ||
      query.state.data?.slicing_queue.some((s) => s.status === 'preparing')
        ? 4_000
        : false,
  })
}

/** Replace one bundle in every cached feed (or drop it when `next` is null). */
function useBundlePatch() {
  const queryClient = useQueryClient()
  return (key: string, next: TriageBundle | null) => {
    queryClient.setQueriesData<S['TriageView']>({ queryKey: KEY }, (view) => {
      if (!view) return view
      const bundles = next
        ? view.bundles.map((b) => (b.key === key ? next : b))
        : view.bundles.filter((b) => b.key !== key)
      return { ...view, bundles }
    })
    refreshReviewState(queryClient, { documents: true })
    queryClient.invalidateQueries({ queryKey: ['shell'] })
  }
}

const batchPath = (id: number) => ({ params: { path: { batch_id: id } } })
const docPath = (id: number) => ({ params: { path: { doc_id: id } } })

export function useBundleAction() {
  const patch = useBundlePatch()
  return useMutation<unknown, ApiError, { bundle: TriageBundle; action: 'dismiss' | 'delete' }>({
    mutationFn: async ({ bundle, action }) => {
      if (bundle.batch_id !== null) {
        await unwrap(
          action === 'dismiss'
            ? api.POST('/api/v1/triage/bundles/{batch_id}/dismiss', batchPath(bundle.batch_id))
            : api.DELETE('/api/v1/triage/bundles/{batch_id}', batchPath(bundle.batch_id)),
        )
      } else {
        const docId = bundle.lead_doc_id ?? 0
        await unwrap(
          action === 'dismiss'
            ? api.POST('/api/v1/triage/documents/{doc_id}/dismiss', docPath(docId))
            : api.DELETE('/api/v1/triage/documents/{doc_id}', docPath(docId)),
        )
      }
    },
    onSuccess: (_, { bundle }) => patch(bundle.key, null),
  })
}

export function useRetryBundle() {
  const patch = useBundlePatch()
  return useMutation<TriageBundle, ApiError, number>({
    mutationFn: (batchId) =>
      unwrap(api.POST('/api/v1/triage/bundles/{batch_id}/retry', batchPath(batchId))),
    onSuccess: (bundle) => patch(bundle.key, bundle),
  })
}

export function useRetryAll() {
  const queryClient = useQueryClient()
  return useMutation<S['RetryAllResult'], ApiError>({
    mutationFn: () => unwrap(api.POST('/api/v1/triage/retry-all')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
  })
}

export function useConfirmBundle() {
  const patch = useBundlePatch()
  return useMutation<S['TriageConfirmResult'], ApiError, S['TriageConfirm'] & { key: string }>({
    mutationFn: ({ key, ...body }) => {
      void key
      return unwrap(api.POST('/api/v1/triage/confirm', { body }))
    },
    onSuccess: (result, { key }) => patch(key, result.bundle),
  })
}

export function useBatchConfirm() {
  const queryClient = useQueryClient()
  return useMutation<S['BatchResult'], ApiError, string[]>({
    mutationFn: (keys) => unwrap(api.POST('/api/v1/triage/batch/confirm', { body: { keys } })),
    onSuccess: () => {
      refreshReviewState(queryClient, { documents: true })
      queryClient.invalidateQueries({ queryKey: ['shell'] })
    },
  })
}

export function useBatchAssign() {
  const queryClient = useQueryClient()
  return useMutation<S['BatchResult'], ApiError, S['BatchAssign']>({
    mutationFn: (body) => unwrap(api.POST('/api/v1/triage/batch/assign', { body })),
    onSuccess: () => {
      refreshReviewState(queryClient, { documents: true })
      queryClient.invalidateQueries({ queryKey: ['shell'] })
    },
  })
}

export function useSetTitle() {
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError, { docId: number; title: string }>({
    mutationFn: ({ docId, title }) =>
      unwrap(
        api.PUT('/api/v1/triage/documents/{doc_id}/title', { ...docPath(docId), body: { title } }),
      ),
    onSuccess: (_, { docId }) => {
      refreshReviewState(queryClient)
      queryClient.invalidateQueries({ queryKey: ['document', docId] })
    },
  })
}

type GroupOp =
  | { op: 'new' }
  | { op: 'reset' }
  | { op: 'cover'; docId: number }
  | { op: 'rename'; subGroupId: number; label: string }
  | { op: 'delete'; subGroupId: number }
  | { op: 'order'; subGroupId: number; docIds: number[] }

export function useGroupOp() {
  const patch = useBundlePatch()
  return useMutation<TriageBundle, ApiError, { batchId: number } & GroupOp>({
    mutationFn: ({ batchId, ...op }) => {
      const p = batchPath(batchId)
      switch (op.op) {
        case 'new':
          return unwrap(api.POST('/api/v1/triage/bundles/{batch_id}/groups', p))
        case 'reset':
          return unwrap(api.POST('/api/v1/triage/bundles/{batch_id}/groups/reset', p))
        case 'cover':
          return unwrap(
            api.PUT('/api/v1/triage/bundles/{batch_id}/cover', {
              ...p,
              body: { doc_id: op.docId },
            }),
          )
        case 'rename':
          return unwrap(
            api.PUT('/api/v1/triage/bundles/{batch_id}/groups/{sub_group_id}', {
              params: { path: { batch_id: batchId, sub_group_id: op.subGroupId } },
              body: { label: op.label },
            }),
          )
        case 'delete':
          return unwrap(
            api.DELETE('/api/v1/triage/bundles/{batch_id}/groups/{sub_group_id}', {
              params: { path: { batch_id: batchId, sub_group_id: op.subGroupId } },
            }),
          )
        case 'order':
          return unwrap(
            api.PUT('/api/v1/triage/bundles/{batch_id}/groups/{sub_group_id}/order', {
              params: { path: { batch_id: batchId, sub_group_id: op.subGroupId } },
              body: { doc_ids: op.docIds },
            }),
          )
      }
    },
    onSuccess: (bundle) => patch(bundle.key, bundle),
  })
}

// --- Document review ---------------------------------------------------------

/** Shared by `useDocumentReview` and batched `useQueries` so both hit the same cache entry. */
export function documentReviewOptions(docId: number | null) {
  return {
    queryKey: ['document', docId],
    queryFn: () => unwrap(api.GET('/api/v1/documents/{doc_id}/review', docPath(docId ?? 0))),
    enabled: docId !== null,
    refetchInterval: (query: { state: { data?: S['DocumentReview'] } }) =>
      ['pending', 'running', 'partial'].includes(query.state.data?.pipeline.state ?? '')
        ? 4_000
        : false,
  }
}

export function useDocumentReview(docId: number | null) {
  return useQuery<S['DocumentReview'], ApiError>(documentReviewOptions(docId))
}

/**
 * Patch every cached view of this document: the review (`['document', id]`)
 * and the reader (`['document', id, 'reader']`), which extends the review.
 */
function useDocPatch(docId: number) {
  const queryClient = useQueryClient()
  return (update: (prev: S['DocumentReview']) => S['DocumentReview']) =>
    queryClient.setQueriesData<S['DocumentReview']>({ queryKey: ['document', docId] }, (prev) =>
      prev ? update(prev) : prev,
    )
}

export function useUpdateMetadata(docId: number) {
  const queryClient = useQueryClient()
  return useMutation<S['DocumentReview'], ApiError, S['MetadataUpdate']>({
    mutationFn: (body) =>
      unwrap(api.PUT('/api/v1/documents/{doc_id}/metadata', { ...docPath(docId), body })),
    onSuccess: (view) => {
      queryClient.setQueriesData<S['DocumentReview']>({ queryKey: ['document', docId] }, (prev) =>
        prev ? { ...prev, ...view } : prev,
      )
      refreshReviewState(queryClient)
    },
  })
}

export function useSummaryAction(docId: number) {
  const patch = useDocPatch(docId)
  const queryClient = useQueryClient()
  return useMutation<S['SummaryView'], ApiError, S['SummaryAction']['action']>({
    mutationFn: (action) =>
      unwrap(
        api.POST('/api/v1/documents/{doc_id}/summary', { ...docPath(docId), body: { action } }),
      ),
    onSuccess: (summary) => {
      patch((prev) => ({ ...prev, summary }))
      // The bundle row's "summaries to approve" and the readiness state follow it.
      refreshReviewState(queryClient)
    },
  })
}

export function useReact(docId: number) {
  const patch = useDocPatch(docId)
  return useMutation<S['ReactionView'][], ApiError, S['ReactionUpdate']>({
    mutationFn: (body) =>
      unwrap(api.POST('/api/v1/documents/{doc_id}/reactions', { ...docPath(docId), body })),
    onSuccess: (reactions) => patch((prev) => ({ ...prev, reactions })),
  })
}

export function useActionStatus(docId: number) {
  const patch = useDocPatch(docId)
  return useMutation<
    S['ActionView'],
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
    onSuccess: (item) =>
      patch((prev) => ({
        ...prev,
        actions: prev.actions.map((a) => (a.id === item.id ? item : a)),
      })),
  })
}

export function useRelationshipDecision(docId: number) {
  const patch = useDocPatch(docId)
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError, { relId: number; decision: 'confirm' | 'reject' }>({
    mutationFn: async ({ relId, decision }) => {
      const p = { params: { path: { rel_id: relId } } }
      await unwrap(
        decision === 'confirm'
          ? api.POST('/api/v1/relationships/{rel_id}/confirm', p)
          : api.DELETE('/api/v1/relationships/{rel_id}', p),
      )
    },
    onSuccess: (_, { relId, decision }) => {
      patch((prev) => ({
        ...prev,
        relationships:
          decision === 'reject'
            ? prev.relationships.filter((r) => r.id !== relId)
            : prev.relationships.map((r) =>
                r.id === relId ? { ...r, confidence: 'user_confirmed' } : r,
              ),
      }))
      // The decision can clear review reasons, here and on the bundle row.
      refreshReviewState(queryClient, { documents: true })
    },
  })
}

/** Dismiss the AI's "this contradicts something" flag on a document. */
export function useAcknowledgeContradiction(docId: number) {
  const queryClient = useQueryClient()
  return useMutation<S['DocumentReview'], ApiError, undefined>({
    mutationFn: () =>
      unwrap(api.POST('/api/v1/documents/{doc_id}/contradiction/acknowledge', docPath(docId))),
    onSuccess: (view) => {
      queryClient.setQueriesData<S['DocumentReview']>({ queryKey: ['document', docId] }, (prev) =>
        prev ? { ...prev, ...view } : prev,
      )
      refreshReviewState(queryClient)
    },
  })
}

/** Confirm or dismiss an AI-proposed claim link (CONTESTS / REFUTES / ...) from the review pane. */
export function useEvidenceDecision() {
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError, { proposalId: number; decision: 'confirm' | 'dismiss' }>({
    mutationFn: ({ proposalId, decision }) => {
      const p = { params: { path: { proposal_id: proposalId } } }
      return unwrap(
        decision === 'confirm'
          ? api.POST('/api/v1/claims/proposals/evidence/{proposal_id}/confirm', p)
          : api.POST('/api/v1/claims/proposals/evidence/{proposal_id}/dismiss', p),
      )
    },
    onSuccess: () => refreshReviewState(queryClient, { documents: true }),
  })
}

export function useStageRetry(docId: number) {
  const patch = useDocPatch(docId)
  const queryClient = useQueryClient()
  return useMutation<S['PipelineView'], ApiError, S['PipelineStage'] | 'all'>({
    mutationFn: (stage) =>
      stage === 'all'
        ? unwrap(api.POST('/api/v1/documents/{doc_id}/pipeline/retry-all', docPath(docId)))
        : unwrap(
            api.POST('/api/v1/documents/{doc_id}/pipeline/{stage}/retry', {
              params: { path: { doc_id: docId, stage } },
            }),
          ),
    onSuccess: (pipeline) => {
      patch((prev) => ({ ...prev, pipeline }))
      queryClient.invalidateQueries({ queryKey: KEY })
    },
  })
}

export function useDraftDecision() {
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError, { caseId: string; decision: 'confirm' | 'reject' }>({
    mutationFn: async ({ caseId, decision }) => {
      const p = { params: { path: { case_id: caseId } } }
      await unwrap(
        decision === 'confirm'
          ? api.POST('/api/v1/cases/{case_id}/confirm-draft', p)
          : api.POST('/api/v1/cases/{case_id}/reject-draft', p),
      )
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KEY })
      queryClient.invalidateQueries({ queryKey: ['document'] })
    },
  })
}

// --- Upload and slicing ------------------------------------------------------

export function useUploadTarget(caseId: string | null) {
  return useQuery<S['UploadTarget'], ApiError>({
    queryKey: ['upload-target', caseId],
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/upload/target', { params: { query: { case_id: caseId ?? undefined } } }),
      ),
    enabled: caseId !== null,
  })
}

export function useUpload() {
  const queryClient = useQueryClient()
  return useMutation<
    S['UploadResponse'],
    ApiError,
    { files: File[]; caseId: string | null; parentId: number | null; splitScans?: boolean }
  >({
    mutationFn: async ({ files, caseId, parentId, splitScans }) => {
      const form = new FormData()
      files.forEach((f) => form.append('files', f))
      if (caseId) form.append('case_id', caseId)
      if (parentId) form.append('parent_id', String(parentId))
      if (splitScans) form.append('split_scans', 'true')
      const response = await fetch('/api/v1/upload', {
        method: 'POST',
        body: form,
        credentials: 'same-origin',
      })
      const body: unknown = await response.json().catch(() => null)
      if (!response.ok) {
        const err = body as { detail?: string; code?: string } | null
        throw new ApiError(response.status, err?.code ?? 'error', err?.detail ?? 'Upload failed')
      }
      return body as S['UploadResponse']
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KEY })
      queryClient.invalidateQueries({ queryKey: ['shell'] })
      queryClient.invalidateQueries({ queryKey: ['worker-queue'] })
    },
  })
}

export function useDocumentStatus(docId: number) {
  return useQuery<S['DocumentStatus'], ApiError>({
    queryKey: ['document-status', docId],
    queryFn: () => unwrap(api.GET('/api/v1/documents/{doc_id}/status', docPath(docId))),
    refetchInterval: (query) =>
      ['completed', 'failed', 'dismissed'].includes(query.state.data?.state ?? '') ? false : 2_000,
  })
}

export function useSlicing(batchId: number) {
  return useQuery<S['SlicingView'], ApiError>({
    queryKey: ['slicing', batchId],
    queryFn: () => unwrap(api.GET('/api/v1/slicing/{batch_id}', batchPath(batchId))),
    refetchInterval: (query) => (query.state.data?.status === 'preparing' ? 3_000 : false),
  })
}

export function useSlicingConfirm(batchId: number) {
  return useMutation<S['SlicingConfirmed'], ApiError, S['SliceCut'][]>({
    mutationFn: (cuts) =>
      unwrap(
        api.POST('/api/v1/slicing/{batch_id}/confirm', { ...batchPath(batchId), body: { cuts } }),
      ),
  })
}

export function useSlicingRetry(batchId: number) {
  const queryClient = useQueryClient()
  return useMutation<S['SlicingView'], ApiError>({
    mutationFn: () => unwrap(api.POST('/api/v1/slicing/{batch_id}/retry', batchPath(batchId))),
    onSuccess: (view) => queryClient.setQueryData(['slicing', batchId], view),
  })
}
