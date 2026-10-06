import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import { conversation } from '../../test/fixtures'
import { renderAt } from '../../test/render'
import { ChatDrawer } from './ChatDrawer'

function sse(frames: object[]) {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(`data: ${JSON.stringify(f)}\n\n`))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

test('streams an answer token by token and links citations into the reader', async () => {
  const fetch = vi.fn(async (request: Request) => {
    const key = `${request.method} ${new URL(request.url).pathname}`
    if (key === 'POST /api/v1/chat/conversations') return Response.json(conversation)
    if (key === 'GET /api/v1/chat/conversations/31') return Response.json(conversation)
    if (key === 'GET /api/v1/chat/conversations') return Response.json([])
    if (key === 'PUT /api/v1/chat/conversations/31/title')
      return Response.json({
        id: 31,
        title: 'What is contested?',
        created_at: '2026-10-06T08:00:00Z',
      })
    if (key === 'POST /api/v1/chat/conversations/31/messages')
      return sse([
        { type: 'token', t: 'The father ' },
        { type: 'token', t: 'disputes custody [DOC:2211#p=1].' },
        {
          type: 'citations',
          docs: [
            { doc_id: 2211, case_id: 'ADV-024-A', title: 'Klageerwiderung', passage_id: 'p1' },
          ],
        },
        { type: 'done' },
      ])
    throw new Error(`unexpected ${key}`)
  })
  vi.stubGlobal('fetch', fetch)
  renderAt(
    '/document/2211',
    <ChatDrawer
      scope={{ scope_type: 'document', scope_id: '2211' }}
      title="Ask about this document"
      suggestions={['What is contested?']}
      onClose={() => {}}
    />,
  )
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'What is contested?' }))
  await waitFor(() => expect(screen.getByText(/disputes custody/)).toBeVisible())
  expect(screen.getByRole('link', { name: '[DOC:2211#p=1]' })).toHaveAttribute(
    'href',
    '/document/2211',
  )
  expect(screen.getByRole('link', { name: /ADV-024-A · #2211 · passage/ })).toHaveAttribute(
    'href',
    '/document/2211#p=p1',
  )
  const sent = fetch.mock.calls.map(([r]) => r).find((r) => r.url.endsWith('/messages'))
  expect(await sent?.clone().json()).toEqual({ content: 'What is contested?', proceeding_id: null })
  await waitFor(() =>
    expect(fetch.mock.calls.some(([r]) => r.method === 'PUT' && r.url.endsWith('/title'))).toBe(
      true,
    ),
  )
})

test('a failed stream shows an error and keeps the question', async () => {
  const fetch = vi.fn(async (request: Request) => {
    const key = `${request.method} ${new URL(request.url).pathname}`
    if (key === 'POST /api/v1/chat/conversations') return Response.json(conversation)
    if (key === 'GET /api/v1/chat/conversations/31') return Response.json(conversation)
    if (key === 'GET /api/v1/chat/conversations') return Response.json([])
    if (key === 'POST /api/v1/chat/conversations/31/messages')
      return Response.json({ detail: 'Too many requests.', code: 'rate_limited' }, { status: 429 })
    throw new Error(`unexpected ${key}`)
  })
  vi.stubGlobal('fetch', fetch)
  renderAt(
    '/document/2211',
    <ChatDrawer
      scope={{ scope_type: 'document', scope_id: '2211' }}
      title="Ask"
      suggestions={[]}
      onClose={() => {}}
    />,
  )
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Message'), 'Hello{Enter}')
  expect(await screen.findByRole('alert')).toHaveTextContent('Too many requests.')
  expect(screen.getByText('Hello')).toBeVisible()
})
