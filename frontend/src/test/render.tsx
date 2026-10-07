import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { vi } from 'vitest'

import { ShortcutsProvider } from '../shell/shortcuts'
import { ToastProvider } from '../ui/toast'

/**
 * Render a page inside the providers the app gives it, at `path`.
 * Pass `layout` to render `ui` as that layout's outlet child.
 */
export function renderAt(path: string, ui: ReactElement, layout?: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const tree = layout ? (
    <Routes>
      <Route element={layout}>
        <Route path="*" element={ui} />
      </Route>
    </Routes>
  ) : (
    ui
  )
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <ShortcutsProvider>
          <MemoryRouter initialEntries={[path]}>{tree}</MemoryRouter>
        </ShortcutsProvider>
      </ToastProvider>
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
