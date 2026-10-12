import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { expect, test } from 'vitest'

import { caseDetail, documentReview } from '../../../test/fixtures'
import { renderAt, stubApi } from '../../../test/render'
import { CasePage } from './CasePage'

const AGENT = 'Claude Code'

const truthMap = (agent: string | null) => ({
  filter: 'open',
  open_claim_count: 1,
  pending_evidence: [],
  pending_merges: [],
  pipeline_active_doc_count: 0,
  dedup_job: null,
  groups: [
    {
      status: 'asserted',
      claims: [
        {
          id: 1,
          claim_text: 'Die Zahlung ist nicht erfolgt.',
          claim_type: 'factual',
          status: 'asserted',
          is_precedent: false,
          first_made_at: '2026-10-01T00:00:00Z',
          last_updated_at: '2026-10-01T00:00:00Z',
          allowed_transitions: [],
          agent_token_name: agent,
          evidence: [
            {
              id: 11,
              role: 'asserts',
              excerpt: 'keine Zahlung',
              confidence: 'user_created',
              document_id: 2211,
              document_title: 'Klageerwiderung.pdf',
              document_originator: 'opposing',
              document_date: '2026-10-01T00:00:00Z',
              reactions: [],
              agent_token_name: agent,
            },
          ],
        },
      ],
    },
  ],
})

function page(path: string, extra: Parameters<typeof stubApi>[0] = {}) {
  stubApi({
    'GET /api/v1/cases/ADV-024-A': { body: caseDetail },
    'GET /api/v1/documents/2211/review': { body: documentReview },
    'POST /api/v1/cases/ADV-024-A/viewed': { status: 204, body: null },
    ...extra,
  })
  renderAt(
    path,
    <Routes>
      <Route path="/cases/:caseId" element={<CasePage />} />
    </Routes>,
  )
}

test('the brief rail marks a deadline an agent added', async () => {
  const [item] = caseDetail.action_items
  if (!item) throw new Error('fixture lacks an action item')
  page('/cases/ADV-024-A?view=review', {
    'GET /api/v1/cases/ADV-024-A': {
      body: { ...caseDetail, action_items: [{ ...item, agent_token_name: AGENT }] },
    },
  })
  const rail = await screen.findByRole('complementary', { name: 'Case brief' })
  expect(within(rail).getByText(`via agent · ${AGENT}`)).toBeVisible()
})

test('the brief rail shows no badge for the user’s own deadline', async () => {
  page('/cases/ADV-024-A?view=review')
  const rail = await screen.findByRole('complementary', { name: 'Case brief' })
  expect(within(rail).getByText('Erwiderung einreichen')).toBeVisible()
  expect(within(rail).queryByText(/via agent/)).not.toBeInTheDocument()
})

test('the Truth Map marks an agent’s claim and its evidence', async () => {
  page('/cases/ADV-024-A?view=truth', {
    'GET /api/v1/cases/ADV-024-A/truthmap': { body: truthMap(AGENT) },
  })
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'show' }))
  expect(await screen.findAllByText(`via agent · ${AGENT}`)).toHaveLength(2)
})

test('the Truth Map marks nothing for the user’s own claim', async () => {
  page('/cases/ADV-024-A?view=truth', {
    'GET /api/v1/cases/ADV-024-A/truthmap': { body: truthMap(null) },
  })
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'show' }))
  expect(await screen.findByText('keine Zahlung', { exact: false })).toBeVisible()
  expect(screen.queryByText(/via agent/)).not.toBeInTheDocument()
})
