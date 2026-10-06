import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router'
import { vi } from 'vitest'

/** Render a page inside the providers the app gives it, at `path`. */
export function renderAt(path: string, ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

type Reply = { status?: number; body: unknown }

/**
 * Stub `fetch` with canned JSON replies keyed by `"METHOD /path"`.
 * Returns the mock so tests can assert on the requests that were sent.
 */
export function stubApi(replies: Record<string, Reply>) {
  const mock = vi.fn(async (request: Request) => {
    const key = `${request.method} ${new URL(request.url).pathname}`
    const reply = replies[key]
    if (!reply) throw new Error(`unexpected request: ${key}`)
    const status = reply.status ?? 200
    if (reply.body === null) return new Response(null, { status })
    return Response.json(reply.body, { status })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}
