import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { emptyQueue, homeView } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { HomePage } from './HomePage'

test('renders greeting, KPIs and every panel from the home view', async () => {
  stubApi({
    'GET /api/v1/home': { body: homeView },
    'GET /api/v1/worker-queue': { body: emptyQueue },
  })
  renderAt('/', <HomePage />)

  expect(await screen.findByRole('heading', { name: 'Good morning, Katharina.' })).toBeVisible()
  expect(screen.getAllByText('File counter-statement')).toHaveLength(2) // deadline + case card
  expect(screen.getByText('Klageerwiderung')).toBeVisible()
  expect(screen.getByText(/AI suggested/)).toBeVisible()
  expect(screen.getByText('1 processing · 0 queued')).toBeVisible()
  expect(screen.getByText('Brandt GmbH ./. Keller')).toBeVisible()
  expect(screen.getByText('Nachlass Hoffmann is dormant')).toBeVisible()
  expect(screen.getByRole('link', { name: /A\. Weber vs\. M\. Weber/ })).toHaveAttribute(
    'href',
    '/cases/ADV-024-A',
  )
  expect(screen.getByText('€18.4k')).toBeVisible()
})

test('review all posts and refreshes the delta panel', async () => {
  const fetch = stubApi({
    'GET /api/v1/home': { body: homeView },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'POST /api/v1/home/review-all': { status: 204, body: null },
  })
  renderAt('/', <HomePage />)
  await userEvent.click(await screen.findByRole('button', { name: 'Review all' }))
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(([r]) => r.method === 'POST' && r.url.endsWith('/review-all')),
    ).toBe(true),
  )
})

test('shows the caught-up state instead of the panels', async () => {
  stubApi({
    'GET /api/v1/home': {
      body: {
        ...homeView,
        today_items: [],
        triage_bundles: [],
        delta_cases: [],
        signals: [],
        caught_up: true,
      },
    },
    'GET /api/v1/worker-queue': { body: emptyQueue },
  })
  renderAt('/', <HomePage />)
  expect(await screen.findByText("You're caught up.")).toBeVisible()
  expect(screen.queryByText('Needs you')).not.toBeInTheDocument()
})
