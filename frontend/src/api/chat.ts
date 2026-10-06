import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError, type Schemas, unwrap } from './client'

type S = Schemas
export type ChatScope = { scope_type: 'document' | 'case'; scope_id: string }
export type Conversation = S['ConversationDetail']
/** One `[DOC:n#p=k]` reference, resolved server-side (app/schemas/chat.py `Citation`). */
export type Citation = {
  doc_id: number
  case_id: string | null
  title: string
  passage_id: string | null
}

export type StreamFrame =
  { type: 'token'; t: string } | { type: 'citations'; docs: Citation[] } | { type: 'done' }

const listKey = (scope: ChatScope) => ['chat', scope.scope_type, scope.scope_id] as const
const convKey = (id: number) => ['chat', 'conversation', id] as const

export function useConversations(scope: ChatScope, enabled = true) {
  return useQuery<S['ConversationSummary'][], ApiError>({
    queryKey: listKey(scope),
    queryFn: () => unwrap(api.GET('/api/v1/chat/conversations', { params: { query: scope } })),
    enabled,
  })
}

export function useConversation(id: number | null) {
  return useQuery<Conversation, ApiError>({
    queryKey: convKey(id ?? 0),
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/chat/conversations/{conversation_id}', {
          params: { path: { conversation_id: id ?? 0 } },
        }),
      ),
    enabled: id !== null,
  })
}

/** The latest conversation for the scope, or a fresh one with `force_new`. */
export function useOpenConversation(scope: ChatScope) {
  const queryClient = useQueryClient()
  return useMutation<Conversation, ApiError, { force_new: boolean }>({
    mutationFn: ({ force_new }) =>
      unwrap(api.POST('/api/v1/chat/conversations', { body: { ...scope, force_new } })),
    onSuccess: (conv) => {
      queryClient.setQueryData(convKey(conv.id), conv)
      queryClient.invalidateQueries({ queryKey: listKey(scope) })
    },
  })
}

export function useRenameConversation(scope: ChatScope) {
  const queryClient = useQueryClient()
  return useMutation<S['ConversationSummary'], ApiError, { id: number; title: string }>({
    mutationFn: ({ id, title }) =>
      unwrap(
        api.PUT('/api/v1/chat/conversations/{conversation_id}/title', {
          params: { path: { conversation_id: id } },
          body: { title },
        }),
      ),
    onSuccess: (summary) => {
      queryClient.setQueryData<Conversation>(convKey(summary.id), (prev) =>
        prev ? { ...prev, title: summary.title } : prev,
      )
      queryClient.invalidateQueries({ queryKey: listKey(scope) })
    },
  })
}

export function useDeleteConversation(scope: ChatScope) {
  const queryClient = useQueryClient()
  return useMutation<unknown, ApiError, number>({
    mutationFn: (id) =>
      unwrap(
        api.DELETE('/api/v1/chat/conversations/{conversation_id}', {
          params: { path: { conversation_id: id } },
        }),
      ),
    onSuccess: (_, id) => {
      queryClient.removeQueries({ queryKey: convKey(id) })
      queryClient.invalidateQueries({ queryKey: listKey(scope) })
    },
  })
}

/**
 * Send a message and consume the server-sent event stream frame by frame.
 * Resolves when the server sends `done` or closes the stream.
 */
export async function streamMessage(
  conversationId: number,
  body: S['MessageSend'],
  onFrame: (frame: StreamFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response
  try {
    response = await globalThis.fetch(
      new Request(
        `${window.location.origin}/api/v1/chat/conversations/${conversationId}/messages`,
        {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify(body),
          signal,
        },
      ),
    )
  } catch (error) {
    if (signal?.aborted) return
    throw new ApiError(0, 'network_error', `Could not reach the server. ${String(error)}`)
  }
  if (!response.ok) {
    let detail = 'Something went wrong. Please try again.'
    let code = 'error'
    try {
      const err = (await response.json()) as Partial<S['ErrorResponse']>
      if (err.detail) detail = err.detail
      if (err.code) code = err.code
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, code, detail)
  }
  if (!response.body) return
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split('\n\n')
    buffer = events.pop() ?? ''
    for (const event of events) {
      for (const line of event.split('\n')) {
        if (!line.startsWith('data: ')) continue
        const frame = JSON.parse(line.slice(6)) as StreamFrame
        onFrame(frame)
        if (frame.type === 'done') return
      }
    }
  }
}

/** Persisted assistant messages carry document ids only; show them as plain citations. */
export function citationsFromIds(ids: number[] | null | undefined): Citation[] {
  return (ids ?? []).map((doc_id) => ({
    doc_id,
    case_id: null,
    title: `#${doc_id}`,
    passage_id: null,
  }))
}
