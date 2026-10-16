import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { expect, test, vi } from 'vitest'

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

test('a document the AI read in several parts says so', async () => {
  stub({
    'GET /api/v1/documents/2211/reader': {
      body: { ...documentReader, context_strategy: 'chunked', context_windows: 3 },
    },
  })
  page()
  expect(await screen.findByText('read in 3 parts')).toBeVisible()
})

test('a document cut short by the window cap says the rest was not read', async () => {
  stub({
    'GET /api/v1/documents/2211/reader': {
      body: {
        ...documentReader,
        context_strategy: 'chunked',
        context_windows: 8,
        context_truncated: true,
      },
    },
  })
  page()
  expect(await screen.findByText(/read in 8 parts, the rest not read/)).toBeVisible()
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

test('says so when the OCR cross-check did not run', async () => {
  stub({
    'GET /api/v1/documents/2211/reader': {
      body: {
        ...documentReader,
        pipeline: { ...documentReader.pipeline, ocr_crosscheck_unavailable: true },
      },
    },
  })
  page()
  expect(await screen.findByTestId('ocr-crosscheck-unavailable')).toHaveTextContent(
    'cross-check did not run',
  )
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

test('the Aktenzeichen is editable and saved with the metadata', async () => {
  const fetch = stub({
    'PUT /api/v1/documents/2211/metadata': { body: { ...documentReader, az_court: '3 F 2022/23' } },
  })
  page()
  const user = userEvent.setup()
  await screen.findByRole('heading', { name: 'Klageerwiderung.pdf' })
  const edit = screen.getAllByRole('button', { name: 'Edit' }).at(0)
  if (!edit) throw new Error('no Edit button')
  await user.click(edit)

  const az = await screen.findByRole('textbox', { name: 'AZ' })
  expect(az).not.toHaveAttribute('readonly')
  await user.clear(az)
  await user.type(az, '3 F 2022/23')
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Save' }))

  await waitFor(async () => {
    const put = fetch.mock.calls.map(([r]) => r).find((r) => r.method === 'PUT')
    if (!put) throw new Error('no PUT yet')
    expect((await put.clone().json()).az_court).toBe('3 F 2022/23')
  })
})

test('says where the case and proceeding were taken over from', async () => {
  stub({
    'GET /api/v1/documents/2211/reader': {
      body: {
        ...documentReader,
        reference: {
          case_id: 'ADV-024-A',
          case_source: 'inherited',
          az_court: '3 F 1221/26',
          az_source: 'inherited',
          inherited_from_doc_id: 2210,
          inherited_from_title: 'Antrag auf Bewilligung',
          exhibit_to_title: 'Antrag auf Bewilligung',
        },
      },
    },
  })
  page()
  const note = await screen.findByTestId('reference-note')
  expect(note).toHaveTextContent('Case taken over from “Antrag auf Bewilligung”')
  expect(note).toHaveTextContent('Proceeding 3 F 1221/26 taken over from its bundle')
  expect(note).toHaveTextContent('Exhibit to “Antrag auf Bewilligung”')
})

test('shows no reference note for a document that carries its own', async () => {
  stub()
  page()
  await screen.findByRole('heading', { name: 'Klageerwiderung.pdf' })
  expect(screen.queryByTestId('reference-note')).not.toBeInTheDocument()
})

test('at tablet widths intelligence opens on demand and pins remain editable', async () => {
  const media = window.matchMedia
  vi.spyOn(window, 'matchMedia').mockImplementation((q) => ({ ...media(q), matches: false }))
  stub()
  page()
  const user = userEvent.setup()
  const trigger = await screen.findByRole('button', { name: 'Document intelligence' })
  expect(
    screen.queryByRole('complementary', { name: 'Document intelligence' }),
  ).not.toBeInTheDocument()
  await user.click(screen.getByText('Pins (1)'))
  expect(screen.getByLabelText('Pin note')).toHaveValue('check this')
  await user.click(trigger)
  const dialog = screen.getByRole('dialog', { name: 'Document intelligence' })
  expect(within(dialog).getByRole('complementary')).toBeVisible()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
  expect(screen.getByTestId('document-hud')).toBeVisible()
})

test('tablet reaction shortcuts work while the intelligence drawer is closed', async () => {
  const media = window.matchMedia
  vi.spyOn(window, 'matchMedia').mockImplementation((q) => ({ ...media(q), matches: false }))
  const fetch = stub({
    'POST /api/v1/documents/2211/reactions': {
      body: [{ reaction: 'lies', notes: null, created_at: null }],
    },
  })
  page()
  await screen.findByTestId('document-hud')
  const user = userEvent.setup()
  await user.keyboard('1')
  await waitFor(() =>
    expect(fetch.mock.calls.some(([r]) => r.url.endsWith('/reactions'))).toBe(true),
  )
  const request = fetch.mock.calls.map(([r]) => r).find((r) => r.url.endsWith('/reactions'))
  expect(await request?.clone().json()).toEqual({ reaction: 'lies' })
  await user.keyboard('r')
  const dialog = screen.getByRole('dialog', { name: 'Document intelligence' })
  expect(within(dialog).getByRole('button', { name: '🚩 Lies' })).toHaveFocus()
})

test('resizing preserves a pin draft and focus mode closes a previously opened intelligence drawer', async () => {
  const media = window.matchMedia
  let wide = false
  const listeners = new Set<EventListenerOrEventListenerObject>()
  vi.spyOn(window, 'matchMedia').mockImplementation((q) => ({
    ...media(q),
    get matches() {
      return wide
    },
    addEventListener: (_event: string, listener: EventListenerOrEventListenerObject) => {
      listeners.add(listener)
    },
    removeEventListener: (_event: string, listener: EventListenerOrEventListenerObject) => {
      listeners.delete(listener)
    },
  }))
  stub({
    'PATCH /api/v1/pins/7': { body: { ...documentReader.pins[0], note: 'check this draft' } },
  })
  page()
  const user = userEvent.setup()
  await screen.findByTestId('document-hud')
  await user.click(screen.getByText('Pins (1)'))
  const note = screen.getByLabelText('Pin note')
  await user.type(note, ' draft')
  act(() => {
    wide = true
    listeners.forEach((listener) => {
      if (typeof listener === 'function') listener(new Event('change'))
      else listener.handleEvent(new Event('change'))
    })
  })
  expect(screen.getByLabelText('Pin note')).toBe(note)
  expect(note).toHaveValue('check this draft')
  act(() => {
    wide = false
    listeners.forEach((listener) => {
      if (typeof listener === 'function') listener(new Event('change'))
      else listener.handleEvent(new Event('change'))
    })
  })
  await user.click(screen.getByRole('button', { name: 'Document intelligence' }))
  act(() => {
    wide = true
    listeners.forEach((listener) => {
      if (typeof listener === 'function') listener(new Event('change'))
      else listener.handleEvent(new Event('change'))
    })
  })
  await user.click(screen.getByRole('button', { name: 'Focus mode (f)' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(
    screen.queryByRole('complementary', { name: 'Document intelligence' }),
  ).not.toBeInTheDocument()
})

test.each([false, true])(
  'a nested metadata draft survives resizing an intelligence pane (initial wide: %s)',
  async (initialWide) => {
    const media = window.matchMedia
    let wide = initialWide
    const listeners = new Set<EventListenerOrEventListenerObject>()
    vi.spyOn(window, 'matchMedia').mockImplementation((q) => ({
      ...media(q),
      get matches() {
        return wide
      },
      addEventListener: (_event: string, listener: EventListenerOrEventListenerObject) => {
        listeners.add(listener)
      },
      removeEventListener: (_event: string, listener: EventListenerOrEventListenerObject) => {
        listeners.delete(listener)
      },
    }))
    const resize = (next: boolean) =>
      act(() => {
        wide = next
        listeners.forEach((listener) => {
          if (typeof listener === 'function') listener(new Event('change'))
          else listener.handleEvent(new Event('change'))
        })
      })
    stub()
    page()
    const user = userEvent.setup()
    await screen.findByTestId('document-hud')
    if (!wide) {
      await user.click(screen.getByRole('button', { name: 'Document intelligence' }))
      resize(true)
    }
    const rail = screen.getByRole('complementary', { name: 'Document intelligence' })
    await user.click(within(rail).getByRole('button', { name: /^Edit$/ }))
    const metadata = screen.getByRole('dialog', { name: 'Edit metadata' })
    const az = within(metadata).getByLabelText('AZ')
    await user.type(az, '3 F 123/26')
    resize(false)
    expect(screen.getByRole('dialog', { name: 'Edit metadata' })).toBe(metadata)
    expect(az).toHaveValue('3 F 123/26')
    expect(az).toHaveFocus()
    for (let i = 0; i < 15; i++) {
      await user.tab()
      expect(metadata.contains(document.activeElement)).toBe(true)
    }
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Edit metadata' })).not.toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Document intelligence' })).toBeVisible()
  },
)

test('explains skipped legacy grouping and distinguishes it from completed stages', async () => {
  const reason = 'legacy_source_reimport_required'
  stub({
    'GET /api/v1/documents/2211/reader': {
      body: {
        ...documentReader,
        pipeline: {
          ...documentReader.pipeline,
          stages: [
            { ...documentReader.pipeline.stages[0], status: 'completed' },
            {
              ...documentReader.pipeline.stages[0],
              key: 'batch_analysis',
              label: 'Batch analysis',
              status: 'skipped',
              reason,
            },
          ],
        },
      },
    },
  })
  page()
  expect(
    await screen.findByText('Grouping skipped · Reimport this email before regrouping'),
  ).toBeVisible()
  expect(screen.getByText('1 complete · 1 skipped')).toBeVisible()
  expect(screen.getByRole('img', { name: '1 of 2 stages complete, 1 skipped' })).toBeInTheDocument()
  expect(screen.queryByText(/All 2 stages complete/)).not.toBeInTheDocument()
  expect(
    screen.getByTitle(/Batch analysis: skipped\s+Reimport this email before regrouping/),
  ).toBeInTheDocument()
})

test('skipped-stage tooltips explain internal reason codes', async () => {
  stub({
    'GET /api/v1/documents/2211/reader': {
      body: {
        ...documentReader,
        pipeline: {
          ...documentReader.pipeline,
          stages: [
            {
              ...documentReader.pipeline.stages[0],
              key: 'claims',
              label: 'Claims',
              status: 'skipped',
              reason: 'copy_of',
            },
          ],
        },
      },
    },
  })
  page()
  expect(await screen.findByTitle(/Claims: skipped\s+Duplicate document/)).toBeInTheDocument()
  expect(screen.getByRole('img', { name: '1 of 1 stages' })).toBeInTheDocument()
})

test.each(['no batch (manual upload)', 'single-doc or empty batch'])(
  'keeps normal pipeline completion for grouping skip: %s',
  async (reason) => {
    stub({
      'GET /api/v1/documents/2211/reader': {
        body: {
          ...documentReader,
          pipeline: {
            ...documentReader.pipeline,
            stages: [
              { ...documentReader.pipeline.stages[0], status: 'completed' },
              {
                ...documentReader.pipeline.stages[0],
                key: 'batch_analysis',
                label: 'Batch analysis',
                status: 'skipped',
                reason,
              },
            ],
          },
        },
      },
    })
    page()
    expect(await screen.findByText('All 2 stages complete')).toBeVisible()
    expect(screen.queryByText(/Grouping skipped/)).not.toBeInTheDocument()
    expect(screen.getByRole('img', { name: '2 of 2 stages' })).toBeInTheDocument()
  },
)

test.each(['pending', 'completed'])(
  'does not show a stale skip reason on a %s stage',
  async (status) => {
    stub({
      'GET /api/v1/documents/2211/reader': {
        body: {
          ...documentReader,
          pipeline: {
            ...documentReader.pipeline,
            stages: [
              {
                ...documentReader.pipeline.stages[0],
                key: 'relationships',
                label: 'Relationships',
                status,
                reason: 'batch_analysis_not_completed',
              },
            ],
          },
        },
      },
    })
    page()
    expect(await screen.findByTitle(`Relationships: ${status}`)).toBeInTheDocument()
    expect(screen.queryByTitle(/Document grouping has not finished/)).not.toBeInTheDocument()
  },
)
