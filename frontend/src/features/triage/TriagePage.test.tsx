import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

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

test('a ?bundle= deep link opens that bundle and scrolls it into view', async () => {
  const scrollIntoView = vi.fn()
  Element.prototype.scrollIntoView = scrollIntoView
  stub()
  renderAt('/triage?bundle=batch-42', <TriagePage />)
  expect(await screen.findByText('Bundle contents · 3')).toBeVisible()
  await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1))
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

test('the case card re-routes the bundle from an inline picker', async () => {
  const fetch = stub({
    'POST /api/v1/triage/confirm': {
      body: {
        bundle: null,
        next_doc_id: null,
        case: { id: 'ADV-019-C', title: 'Brandt GmbH ./. Keller', action: 'assigned' },
      },
    },
  })
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /ib-0042/ }))
  await user.click(await screen.findByRole('button', { name: 'Change case' }))
  await user.click(screen.getByRole('menuitem', { name: /ADV-019-C/ }))
  await waitFor(async () =>
    expect(await postedJson(fetch, '/triage/confirm')).toMatchObject({
      action: 'assign_case',
      batch_id: 42,
      case_id: 'ADV-019-C',
      proceeding_id: null,
    }),
  )
  expect(await screen.findByRole('status')).toHaveTextContent('Assigned to ADV-019-C')
})

test('the proceeding picker lists only the assigned case’s proceedings', async () => {
  const fetch = stub({
    'GET /api/v1/documents/2211/review': {
      body: {
        ...documentReview,
        case: { id: 'ADV-024-A', title: 'Weber ./. Weber', is_draft: false },
      },
    },
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
  await user.click(await screen.findByRole('button', { name: /ib-0042/ }))
  await user.click(await screen.findByRole('button', { name: 'Change proceeding' }))
  const items = screen.getAllByRole('menuitem')
  expect(items).toHaveLength(2)
  expect(items[0]).toHaveTextContent('Amtsgericht Hamburg · 003 F 426/25')
  expect(items[1]).toHaveTextContent('— none —')
  await user.click(screen.getByRole('menuitem', { name: /Amtsgericht Hamburg/ }))
  await waitFor(async () =>
    expect(await postedJson(fetch, '/triage/confirm')).toMatchObject({
      case_id: 'ADV-024-A',
      proceeding_id: 5,
    }),
  )
})

test('email-header relationships get a badge, not confirm/reject controls', async () => {
  stub({
    'GET /api/v1/documents/2211/review': {
      body: {
        ...documentReview,
        relationships: [
          {
            id: 501,
            doc_id: 1500,
            title: 'Antragsschrift',
            originator_type: 'opposing',
            rel_type: 'replies_to',
            confidence: 'ai_detected',
            direction: 'out',
          },
          {
            id: 502,
            doc_id: 1501,
            title: 'Beschluss',
            originator_type: 'court',
            rel_type: 'replies_to',
            confidence: 'email_header',
            direction: 'out',
          },
        ],
      },
    },
  })
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /ib-0042/ }))
  expect(await screen.findByText('email header')).toBeInTheDocument()
  // Only the AI suggestion can be confirmed or rejected.
  expect(screen.getAllByRole('button', { name: 'Confirm relationship' })).toHaveLength(1)
  expect(screen.getAllByRole('button', { name: 'Reject relationship' })).toHaveLength(1)
})

test('pending claim links can be confirmed from the Grounds card', async () => {
  const fetch = stub({
    'GET /api/v1/documents/2211/review': {
      body: {
        ...documentReview,
        evidence_proposals: [
          {
            proposal_id: 31,
            proposed_role: 'contests',
            excerpt: 'Der Umgang wurde nie verweigert.',
            target_claim_id: 9,
            target_claim_text: 'Umgang wurde verweigert',
            target_claim_status: 'asserted',
            source_document_id: 2211,
            source_document_title: 'Klageerwiderung.pdf',
          },
        ],
      },
    },
    'POST /api/v1/claims/proposals/evidence/31/confirm': { status: 204, body: null },
  })
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /ib-0042/ }))
  expect(await screen.findByText('Claim links to confirm')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Confirm claim link' }))
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(
        ([r]) => r.method === 'POST' && r.url.endsWith('/claims/proposals/evidence/31/confirm'),
      ),
    ).toBe(true),
  )
})

test('the checklist lists what is left to review on the open document', async () => {
  stub()
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /ib-0042/ }))
  const list = await screen.findByRole('region', { name: 'To review' })
  expect(within(list).getByText(/Check metadata:.*sender.*issued/)).toBeVisible()
  expect(within(list).getByText('1 relationship to confirm')).toBeVisible()
  expect(within(list).getByText('1 claim link to confirm')).toBeVisible()
})

const clearReview = {
  ...documentReview,
  review_reasons: [],
  metadata: documentReview.metadata.map((f) => ({ ...f, confidence: 'high' as const })),
  relationships: [],
  evidence_proposals: [],
}

function withSiblingReasons(reasons: string[]) {
  const [bundle, ...rest] = triageView.bundles
  if (!bundle) throw new Error('fixture has no bundle')
  const documents = bundle.documents.map((d) =>
    d.id === 2210 ? { ...d, review_reasons: reasons } : { ...d, review_reasons: [] },
  )
  return { ...triageView, bundles: [{ ...bundle, documents }, ...rest] }
}

test('the checklist is green when this document and its bundle are clear', async () => {
  stub({
    'GET /api/v1/triage': { body: withSiblingReasons([]) },
    'GET /api/v1/documents/2211/review': { body: clearReview },
  })
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /ib-0042/ }))
  expect(await screen.findByText(/Ready to confirm/)).toBeVisible()
})

test('a clear document is not "ready" while a sibling still has open items', async () => {
  stub({
    'GET /api/v1/triage': { body: withSiblingReasons(['unresolved_relationship']) },
    'GET /api/v1/documents/2211/review': { body: clearReview },
  })
  renderAt('/triage', <TriagePage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /ib-0042/ }))
  expect(await screen.findByText(/This document is clear, but 1 other document/)).toBeVisible()
  expect(screen.queryByText(/Ready to confirm/)).not.toBeInTheDocument()
})
