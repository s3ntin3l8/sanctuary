import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { briefingView, emptyQueue, homeView } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { HomePage } from './HomePage'

test('renders greeting, KPIs and every panel from the home view', async () => {
  stubApi({
    'GET /api/v1/home': { body: homeView },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'GET /api/v1/home/briefing': { body: briefingView },
  })
  renderAt('/', <HomePage />)

  expect(await screen.findByRole('heading', { name: 'Good morning, Katharina.' })).toBeVisible()
  expect(screen.getAllByText('File counter-statement')).toHaveLength(2) // deadline + the lone case card's upcoming list
  expect(screen.getByText(/^Coming up/)).toBeVisible()
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
  const activity = screen.getByRole('region', { name: 'Recent activity' })
  expect(
    within(activity).getByRole('link', { name: /Klageerwiderung\.pdf enriched/ }),
  ).toHaveAttribute('href', '/document/2211')
  expect(within(activity).getByText('metadata stage · file too large')).toBeVisible()
})

test('activity strip shows its empty state', async () => {
  stubApi({
    'GET /api/v1/home': { body: { ...homeView, activity: [] } },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'GET /api/v1/home/briefing': { body: briefingView },
  })
  renderAt('/', <HomePage />)
  await screen.findByRole('heading', { name: 'Good morning, Katharina.' })
  expect(screen.getByText('Nothing has happened yet.')).toBeVisible()
})

test('review all posts and refreshes the delta panel', async () => {
  const fetch = stubApi({
    'GET /api/v1/home': { body: homeView },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'GET /api/v1/home/briefing': { body: briefingView },
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
    'GET /api/v1/home/briefing': { body: briefingView },
  })
  renderAt('/', <HomePage />)
  expect(await screen.findByText("You're caught up.")).toBeVisible()
  expect(screen.queryByText('Needs you')).not.toBeInTheDocument()
})

test('j / k move focus across the panels in order, Enter follows the link, ? lists the keys', async () => {
  stubApi({
    'GET /api/v1/home': { body: homeView },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'GET /api/v1/home/briefing': { body: briefingView },
  })
  renderAt('/', <HomePage />)
  const user = userEvent.setup()
  await screen.findByRole('heading', { name: 'Good morning, Katharina.' })

  await user.keyboard('j')
  // First row: the deadline in "Needs you".
  expect(document.activeElement).toHaveAttribute('href', '/cases/ADV-024-A')
  expect(document.activeElement).toHaveTextContent('File counter-statement')
  await user.keyboard('j')
  // Second: the triage bundle.
  expect(document.activeElement).toHaveAttribute('href', '/triage?bundle=batch-42')
  await user.keyboard('j')
  // Third: the lone case card.
  expect(document.activeElement).toHaveAttribute('href', '/cases/ADV-024-A')
  expect(document.activeElement).toHaveTextContent('Coming up')
  await user.keyboard('kk')
  expect(document.activeElement).toHaveTextContent('File counter-statement')
  // Wraps backwards to the last row (the last activity entry).
  await user.keyboard('k')
  expect(document.activeElement).toHaveTextContent('Gutachten_Anhang_gross.pdf')

  // Typing in a field must not move focus.
  const input = document.createElement('input')
  document.body.appendChild(input)
  input.focus()
  await user.keyboard('j')
  expect(document.activeElement).toBe(input)
  input.remove()

  await user.keyboard('?')
  const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
  expect(within(dialog).getByRole('region', { name: 'Home' })).toBeVisible()
  expect(within(dialog).getByText('Next / previous item across the panels')).toBeVisible()
  await user.keyboard('{Escape}')
  await waitFor(() => expect(dialog).not.toBeInTheDocument())
})

test('the lone case card lists the next few items and counts the rest', async () => {
  const [first] = homeView.today_items
  if (!first) throw new Error('fixture has no deadline')
  const extra = [2, 3, 4, 5, 6].map((id) => ({ ...first, id, title: `Frist ${id}` }))
  stubApi({
    'GET /api/v1/home': { body: { ...homeView, today_items: [first, ...extra] } },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'GET /api/v1/home/briefing': { body: briefingView },
  })
  renderAt('/', <HomePage />)
  const card = (await screen.findByText('Coming up')).closest('a')
  if (!card) throw new Error('lone case card not found')
  const inCard = within(card)
  // The next action stays on the left; three of the five others are listed, two are counted.
  expect(inCard.getByText('Frist 2')).toBeVisible()
  expect(inCard.getByText('Frist 4')).toBeVisible()
  expect(inCard.queryByText('Frist 5')).not.toBeInTheDocument()
  expect(inCard.getByText('+2 more')).toBeVisible()
})

test('a case card with documents to review says so and opens the filtered Review tab', async () => {
  const [card] = homeView.active_cases
  if (!card) throw new Error('fixture has no case')
  stubApi({
    'GET /api/v1/home': {
      body: { ...homeView, active_cases: [{ ...card, to_review_count: 3 }] },
    },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'GET /api/v1/home/briefing': { body: briefingView },
  })
  renderAt('/', <HomePage />)
  const link = await screen.findByRole('link', { name: /A\. Weber vs\. M\. Weber/ })
  expect(within(link).getByText('3 to review')).toBeVisible()
  expect(link).toHaveAttribute('href', '/cases/ADV-024-A?view=review&open=1')
})
