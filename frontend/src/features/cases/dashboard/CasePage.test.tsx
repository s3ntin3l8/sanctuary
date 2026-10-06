import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { expect, test } from 'vitest'

import { caseDetail, documentReview } from '../../../test/fixtures'
import { renderAt, stubApi } from '../../../test/render'
import { CasePage } from './CasePage'

const graph = {
  proceeding_id: 10,
  filter: 'significant+',
  lanes: [{ key: 'own', label: 'YOU', color: 'own' }],
  nodes: [
    {
      id: 2211,
      lane: 'opposing',
      row: 0,
      x: 36,
      y: 32,
      w: 180,
      h: 50,
      title: 'Klageerwider…',
      full_title: 'Klageerwiderung.pdf',
      role: 'standalone',
      date_short: '01-20',
      tier: 'critical',
      thread_open: true,
      ghost: false,
      cross_proceeding: false,
      proceeding_label: null,
      is_bundle: false,
      is_new_since_last_visit: true,
      reaction: null,
      court_relay: false,
      originator_type: 'opposing',
    },
  ],
  bundles: [],
  edges: [],
  proof_badges: {},
  svg_width: 1200,
  svg_height: 300,
  node_counts: { critical: 1, significant: 0, informational: 0, administrative_standalone: 2 },
  node_count: 1,
  edge_count: 0,
}

function stub(extra: Parameters<typeof stubApi>[0] = {}) {
  return stubApi({
    'GET /api/v1/cases/ADV-024-A': { body: caseDetail },
    'GET /api/v1/documents/2211/review': { body: documentReview },
    'GET /api/v1/cases/ADV-024-A/graph': { body: graph },
    ...extra,
  })
}

function page(path = '/cases/ADV-024-A') {
  return renderAt(
    path,
    <Routes>
      <Route path="/cases/:caseId" element={<CasePage />} />
    </Routes>,
  )
}

test('renders the header, spine, review panel and brief rail', async () => {
  stub()
  page()
  expect(await screen.findByRole('heading', { level: 1, name: 'Vane ./. Vane' })).toBeVisible()
  const spine = screen.getByRole('complementary', { name: 'Case spine' })
  expect(within(spine).getAllByRole('button')).toHaveLength(2)
  expect(within(spine).getByRole('button', { name: /Klageerwiderung/ })).toHaveAttribute(
    'aria-current',
    'true',
  )
  expect(await screen.findByText('Directly rebuts the core custody claim.')).toBeVisible()
  const rail = screen.getByRole('complementary', { name: 'Case brief' })
  expect(within(rail).getByText('Client seeks sole custody.')).toBeVisible()
  expect(within(rail).getByText('Erwiderung einreichen')).toBeVisible()
  expect(within(rail).getByText(/€18,450/)).toBeVisible()
  expect(screen.getByRole('button', { name: /Truth map/ })).toHaveTextContent('3')
})

test('switching the proceeding persists it and the graph tab renders nodes', async () => {
  const fetch = stub({
    'PUT /api/v1/cases/ADV-024-A/active-proceeding': { status: 204, body: null },
  })
  page()
  const user = userEvent.setup()
  await screen.findByRole('heading', { level: 1 })
  await user.selectOptions(screen.getByLabelText('Proceeding'), '11')
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(([r]) => r.method === 'PUT' && r.url.endsWith('/active-proceeding')),
    ).toBe(true),
  )
  await user.keyboard('g')
  expect(await screen.findByLabelText('Correspondence graph')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Klageerwiderung.pdf' })).toBeInTheDocument()
  expect(screen.getByText('2 hidden by filter · show all')).toBeVisible()
})

test('draft banner ratifies the case', async () => {
  const fetch = stub({
    'GET /api/v1/cases/ADV-024-A': { body: { ...caseDetail, is_draft: true } },
    'POST /api/v1/cases/ADV-024-A/confirm-draft': {
      body: { id: 'ADV-024-A', title: 'Vane ./. Vane', is_draft: false },
    },
  })
  page()
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Ratify' }))
  await waitFor(() =>
    expect(fetch.mock.calls.some(([r]) => r.url.endsWith('/confirm-draft'))).toBe(true),
  )
})
