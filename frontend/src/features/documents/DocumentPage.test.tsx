import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { expect, test } from 'vitest'

import { conversation, documentReader } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { DocumentPage } from './DocumentPage'

function stub(extra: Parameters<typeof stubApi>[0] = {}) {
  return stubApi({
    'GET /api/v1/documents/2211/reader': { body: documentReader },
    'POST /api/v1/chat/conversations': { body: conversation },
    'GET /api/v1/chat/conversations/31': { body: conversation },
    'GET /api/v1/chat/conversations': { body: [] },
    ...extra,
  })
}

function page() {
  return renderAt(
    '/document/2211',
    <Routes>
      <Route path="/document/:id" element={<DocumentPage />} />
    </Routes>,
  )
}

test('renders the body with highlights, the rail and navigation', async () => {
  stub()
  page()
  expect(await screen.findByRole('heading', { name: 'Klageerwiderung.pdf' })).toBeVisible()
  expect(screen.getByText('Beklagte bestreitet')).toBeVisible()
  expect(screen.getByText('2 / 5')).toBeVisible()
  expect(screen.getByRole('link', { name: 'Next document (→)' })).toHaveAttribute(
    'href',
    '/document/2212',
  )
  expect(screen.getByRole('link', { name: 'Open original' })).toHaveAttribute(
    'href',
    '/api/v1/documents/2211/original',
  )
  expect(screen.getByText('thread open')).toBeVisible()
  expect(screen.getByLabelText('Pin note')).toHaveValue('check this')
  expect(screen.getByRole('complementary', { name: 'Document intelligence' })).toBeVisible()
})

test('clicking a highlight selects its spine row; a pin can be added and removed', async () => {
  const fetch = stub({
    'POST /api/v1/documents/2211/pins': {
      status: 201,
      body: { id: 8, passage_id: 'p1', note: null, user_id: 1, updated_at: null },
    },
    'DELETE /api/v1/pins/7': { status: 204, body: null },
  })
  page()
  const user = userEvent.setup()
  await user.click(await screen.findByText('Beklagte bestreitet'))
  const rail = screen.getByRole('complementary', { name: 'Document intelligence' })
  await waitFor(() =>
    expect(within(rail).getByRole('button', { name: /Der Beklagte bestreitet/ })).toHaveAttribute(
      'aria-current',
      'true',
    ),
  )
  await user.click(within(rail).getByRole('button', { name: 'Pin this passage' }))
  await waitFor(() => expect(screen.getAllByLabelText('Pin note')).toHaveLength(2))
  const posted = fetch.mock.calls.map(([r]) => r).find((r) => r.method === 'POST')
  expect(await posted?.clone().json()).toEqual({ passage_id: 'p1', note: null })
  const [firstDelete] = screen.getAllByRole('button', { name: 'Delete pin' })
  if (!firstDelete) throw new Error('no pin to delete')
  await user.click(firstDelete)
  await waitFor(() => expect(screen.getAllByLabelText('Pin note')).toHaveLength(1))
})

test('focus mode hides the rail and "/" opens the chat with the document scope', async () => {
  const fetch = stub()
  page()
  const user = userEvent.setup()
  await screen.findByRole('heading', { name: 'Klageerwiderung.pdf' })
  await user.keyboard('f')
  expect(screen.queryByRole('complementary', { name: 'Document intelligence' })).toBeNull()
  await user.keyboard('/')
  expect(
    await screen.findByRole('complementary', { name: 'Ask about this document' }),
  ).toBeVisible()
  const opened = fetch.mock.calls
    .map(([r]) => r)
    .find((r) => r.method === 'POST' && r.url.endsWith('/chat/conversations'))
  expect(await opened?.clone().json()).toEqual({
    scope_type: 'document',
    scope_id: '2211',
    force_new: false,
  })
  await user.keyboard('?')
  expect(await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })).toBeVisible()
})

test('find counts matches in the body', async () => {
  stub()
  page()
  const user = userEvent.setup()
  await screen.findByRole('heading', { name: 'Klageerwiderung.pdf' })
  await user.click(screen.getByRole('button', { name: 'Find in document (⌘F)' }))
  await user.type(screen.getByLabelText('Find in document'), 'kosten')
  expect(screen.getByText('1/1')).toBeVisible()
})

test('number keys fire reactions and Esc on the shortcuts overview stays on the page', async () => {
  const fetch = stub({
    'POST /api/v1/documents/2211/reactions': {
      body: [{ reaction: 'lies', notes: null, created_at: null }],
    },
  })
  page()
  const user = userEvent.setup()
  await screen.findByRole('heading', { name: 'Klageerwiderung.pdf' })
  await user.keyboard('1')
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(([r]) => r.method === 'POST' && r.url.endsWith('/reactions')),
    ).toBe(true),
  )
  await user.keyboard('?')
  const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
  await user.keyboard('{Escape}')
  await waitFor(() => expect(dialog).not.toBeInTheDocument())
  expect(screen.getByRole('heading', { name: 'Klageerwiderung.pdf' })).toBeVisible()
})

test('warns about OCR page failures and re-extracts on demand', async () => {
  const fetch = stub({
    'GET /api/v1/documents/2211/reader': {
      body: {
        ...documentReader,
        pipeline: { ...documentReader.pipeline, ocr_page_failures: [1, 3] },
      },
    },
    'POST /api/v1/documents/2211/pipeline/extract/retry': {
      body: { ...documentReader.pipeline, ocr_page_failures: [] },
    },
  })
  page()
  const user = userEvent.setup()
  const alert = await screen.findByTestId('ocr-page-failures')
  expect(alert).toHaveTextContent('OCR failed on pages 1, 3')
  await user.click(within(alert).getByRole('button', { name: 'Re-extract' }))
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(
        ([r]) => r.method === 'POST' && r.url.endsWith('/documents/2211/pipeline/extract/retry'),
      ),
    ).toBe(true),
  )
})
