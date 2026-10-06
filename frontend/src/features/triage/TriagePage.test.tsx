import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { documentReview, triageBundle, triageView } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { TriagePage } from './TriagePage'

function stub(extra: Parameters<typeof stubApi>[0] = {}) {
  return stubApi({
    'GET /api/v1/triage': { body: triageView },
    'GET /api/v1/documents/2211/review': { body: documentReview },
    ...extra,
  })
}

async function postedJson(fetch: ReturnType<typeof stubApi>, suffix: string) {
  const r = fetch.mock.calls
    .map(([x]) => x)
    .find((x) => x.method === 'POST' && x.url.endsWith(suffix))
  return r?.clone().json()
}

test('lists bundles with their state and filters by status chip', async () => {
  stub()
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  expect(await screen.findByText('Klageerwiderung')).toBeVisible()
  expect(screen.getByText('2 pending')).toBeVisible()
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  expect(screen.getByText('Docling conversion failed — file too large.')).toBeVisible()
  await user.click(screen.getByRole('button', { name: /^Stuck/ }))
  expect(screen.getAllByRole('listitem')).toHaveLength(1)
  expect(screen.getByRole('link', { name: '#77' })).toHaveAttribute('href', '/ingest/slice/77')
})

test('confirm uses the AI suggestion and posts the routing', async () => {
  const fetch = stub({
    'POST /api/v1/triage/confirm': {
      body: {
        bundle: null,
        next_doc_id: null,
        case: { id: 'ADV-024-A', title: 'Weber ./. Weber', action: 'assigned' },
      },
    },
  })
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  const row = (await screen.findByText('Klageerwiderung')).closest('li')
  if (!row) throw new Error('row')
  await user.click(within(row).getByRole('button', { name: 'Confirm' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText('AI suggested')).toBeVisible()
  await user.click(within(dialog).getByRole('button', { name: /Confirm & complete/ }))
  await waitFor(async () =>
    expect(await postedJson(fetch, '/triage/confirm')).toMatchObject({
      batch_id: 42,
      action: 'confirm_bundle',
      case_id: 'ADV-024-A',
      proceeding_id: 5,
    }),
  )
  expect(await screen.findByRole('status')).toHaveTextContent('Filed into ADV-024-A')
})

test('expanding a row shows the bundle tree and the review panel', async () => {
  const fetch = stub({
    'POST /api/v1/documents/2211/reactions': {
      body: [{ reaction: 'lies', notes: null, created_at: null }],
    },
  })
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /ib-0042/ }))
  expect(await screen.findByText('Bundle contents · 3')).toBeVisible()
  expect(await screen.findByText('Directly rebuts the core custody claim.')).toBeVisible()
  expect(screen.getByText('Umgang wurde verweigert')).toBeVisible()
  expect(screen.getByText('LLM timeout')).toBeVisible()
  await user.click(screen.getByRole('button', { name: '🚩 Lies' }))
  await waitFor(async () =>
    expect(await postedJson(fetch, '/2211/reactions')).toEqual({ reaction: 'lies' }),
  )
  expect(screen.getByRole('button', { name: '🚩 Lies' })).toHaveAttribute('aria-pressed', 'true')
})

test('batch selection confirms every selected bundle', async () => {
  const fetch = stub({
    'POST /api/v1/triage/batch/confirm': {
      body: { confirmed: 1, skipped: 1, bundles: [], removed_keys: ['batch-42'] },
    },
  })
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  await screen.findByText('Klageerwiderung')
  await user.click(screen.getByRole('checkbox', { name: 'Select all visible' }))
  await user.click(screen.getByRole('button', { name: 'Confirm (2)' }))
  await waitFor(async () =>
    expect(await postedJson(fetch, '/batch/confirm')).toEqual({ keys: ['batch-42', 'batch-39'] }),
  )
})

test('?upload=1 opens the ingest modal', async () => {
  stub()
  renderAt('/triage?upload=1', <TriagePage />)
  expect(await screen.findByRole('dialog', { name: 'Ingest documents' })).toBeVisible()
  expect(screen.getByText(/Drop files here/)).toBeVisible()
})

test('a bundle without a suggestion offers routing', async () => {
  stub({
    'GET /api/v1/triage': {
      body: {
        ...triageView,
        bundles: [{ ...triageBundle, suggestion: null, status: 'needs_classification' }],
      },
    },
  })
  renderAt('/triage', <TriagePage />)
  expect(await screen.findByRole('button', { name: 'Route' })).toBeVisible()
})

test('keyboard: Enter on a row action does not toggle the row, ⌘↵ opens confirm', async () => {
  stub()
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  const row = await screen.findByRole('button', { name: /ib-0042/ })
  row.focus()
  await user.keyboard('{Enter}')
  expect(await screen.findByText('Bundle contents · 3')).toBeVisible()
  const checkbox = within(row).getByRole('checkbox')
  checkbox.focus()
  await user.keyboard('{Enter}')
  expect(screen.getByText('Bundle contents · 3')).toBeVisible()
  await user.keyboard('{Meta>}{Enter}{/Meta}')
  expect(await screen.findByRole('dialog')).toBeVisible()
})
