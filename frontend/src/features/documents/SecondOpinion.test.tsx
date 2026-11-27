import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { expect, test } from 'vitest'

import type { ReviewFinding } from '../../api/reviewFindings'
import { caseDetail, conversation, documentReader } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { CasePage } from '../cases/dashboard/CasePage'
import { DocumentPage } from './DocumentPage'

const base: ReviewFinding = {
  id: 7,
  reviewer_kind: 'agent',
  agent_token_name: 'Claude Code',
  target_kind: 'document_field',
  target_label: 'Significance tier',
  document_id: 2211,
  document_title: 'Klageerwiderung.pdf',
  case_id: 'ADV-024-A',
  target_id: null,
  field: 'significance_tier',
  verdict: 'disagree',
  ai_value: 'informational',
  proposed_value: 'critical',
  rationale: 'The letter sets a two-week deadline.',
  cited_excerpt: 'binnen zwei Wochen',
  reviewed_model: 'qwen3:14b',
  reviewed_prompt_version: '2026-11-27',
  status: 'pending',
  created_at: '2026-10-02T09:00:00',
  resolved_at: null,
}

function documentPage(findings: ReviewFinding[], extra: Parameters<typeof stubApi>[0] = {}) {
  const fetch = stubApi({
    'GET /api/v1/documents/2211/reader': { body: documentReader },
    'GET /api/v1/documents/2211/review-findings': { body: findings },
    'POST /api/v1/chat/conversations': { body: conversation },
    'GET /api/v1/chat/conversations/31': { body: conversation },
    'GET /api/v1/chat/conversations': { body: [] },
    ...extra,
  })
  renderAt(
    '/document/2211',
    <Routes>
      <Route path="/document/:id" element={<DocumentPage />} />
    </Routes>,
  )
  return fetch
}

test('lists a pending disagreement with its values, source and reviewer', async () => {
  documentPage([base])
  expect(await screen.findByText('Second opinion')).toBeVisible()
  const row = screen.getByRole('listitem', { name: 'Significance tier: disagrees' })
  expect(within(row).getByText('informational')).toBeVisible()
  expect(within(row).getByText('critical')).toBeVisible()
  expect(within(row).getByText('The letter sets a two-week deadline.')).toBeVisible()
  expect(within(row).getByText('“binnen zwei Wochen”')).toBeVisible()
  expect(within(row).getByText('via agent · Claude Code')).toBeVisible()
  expect(within(row).getByRole('button', { name: 'Confirm' })).toBeEnabled()
})

test('confirming posts the decision', async () => {
  const fetch = documentPage([base], {
    'POST /api/v1/review-findings/7/confirm': {
      body: { ...base, status: 'confirmed', resolved_at: '2026-10-03T09:00:00' },
    },
  })
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Confirm' }))
  const calls = fetch.mock.calls.map(([r]) => `${r.method} ${new URL(r.url).pathname}`)
  expect(calls).toContain('POST /api/v1/review-findings/7/confirm')
})

test('a stale or resolved finding offers no decision', async () => {
  documentPage([
    { ...base, status: 'stale' },
    {
      ...base,
      id: 8,
      reviewer_kind: 'verifier',
      agent_token_name: null,
      verdict: 'agree',
      status: 'noted',
      proposed_value: null,
      target_label: 'Sender',
    },
  ])
  expect(await screen.findByText('stale')).toBeVisible()
  expect(screen.getByText('via verifier')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
})

test('the case brief shows its stale marker and brief findings', async () => {
  stubApi({
    'GET /api/v1/cases/ADV-024-A': {
      body: { ...caseDetail, brief: { ...caseDetail.brief, stale: true } },
    },
    'GET /api/v1/cases/ADV-024-A/review-findings': {
      body: [
        {
          ...base,
          target_kind: 'case_brief',
          target_label: 'Brief: next move',
          document_id: null,
          document_title: null,
          field: 'next_move',
          ai_value: 'File the rebuttal.',
          proposed_value: 'Wait for the expert report first.',
        },
      ],
    },
    'POST /api/v1/cases/ADV-024-A/viewed': { status: 204, body: null },
  })
  renderAt(
    '/cases/ADV-024-A?view=review',
    <Routes>
      <Route path="/cases/:caseId" element={<CasePage />} />
    </Routes>,
  )
  const rail = await screen.findByRole('complementary', { name: 'Case brief' })
  expect(within(rail).getByText('stale')).toBeVisible()
  expect(await within(rail).findByText('Wait for the expert report first.')).toBeVisible()
})
