import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { caseCard } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { CasesPage } from './CasesPage'

const directory = {
  cases: [
    caseCard,
    {
      ...caseCard,
      id: 'ADV-031-A',
      title: 'Nachlass Hoffmann',
      is_dormant: true,
      days_since_activity: 120,
    },
    { ...caseCard, id: 'OLD-1', title: 'Closed matter', status: 'closed', status_label: 'Closed' },
    { ...caseCard, id: 'ADV-019-C', title: 'Brandt GmbH ./. Keller', pending_close: true },
  ],
  counts_by_status: { pre_trial: 3, closed: 1 },
  total: 4,
}

test('filters by status chip and by text', async () => {
  stubApi({ 'GET /api/v1/cases': { body: directory } })
  renderAt('/cases', <CasesPage />)
  const user = userEvent.setup()

  expect(await screen.findByText('4 total · 2 active')).toBeVisible()
  expect(screen.getAllByRole('listitem')).toHaveLength(2)

  await user.click(screen.getByRole('button', { name: /Dormant/ }))
  expect(screen.getByText('Nachlass Hoffmann')).toBeVisible()
  expect(screen.queryByText('Weber ./. Weber')).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: /All/ }))
  await user.type(screen.getByLabelText('Filter cases'), 'brandt')
  expect(screen.getAllByRole('listitem')).toHaveLength(1)
  expect(screen.getByText('Brandt GmbH ./. Keller')).toBeVisible()
})

test('pending-close banner posts the decision', async () => {
  const fetch = stubApi({
    'GET /api/v1/cases': { body: directory },
    'POST /api/v1/cases/ADV-019-C/confirm-close': { status: 204, body: null },
  })
  renderAt('/cases', <CasesPage />)
  const row = (await screen.findByText('Brandt GmbH ./. Keller')).closest('li')
  if (!row) throw new Error('row not found')
  expect(within(row).getByText('AI suggests closing this case.')).toBeVisible()
  await userEvent.click(within(row).getByRole('button', { name: 'Close case' }))
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(
        ([r]) => r.method === 'POST' && r.url.endsWith('/ADV-019-C/confirm-close'),
      ),
    ).toBe(true),
  )
})

test('new case modal submits and shows server errors', async () => {
  stubApi({
    'GET /api/v1/cases': { body: directory },
    'POST /api/v1/cases': {
      status: 409,
      body: { detail: 'A case with id X already exists.', code: 'case_id_taken' },
    },
  })
  renderAt('/cases', <CasesPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /New case/ }))
  const dialog = screen.getByRole('dialog')
  await user.type(within(dialog).getByLabelText('Case ID'), 'X')
  await user.type(within(dialog).getByLabelText('Title'), 'T')
  await user.type(within(dialog).getByLabelText('Court'), 'AG Hamburg')
  await user.click(within(dialog).getByRole('button', { name: 'Create case' }))
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('already exists')
})

test('a row with documents to review opens the filtered Review tab', async () => {
  stubApi({
    'GET /api/v1/cases': {
      body: { ...directory, cases: [{ ...caseCard, to_review_count: 2 }] },
    },
  })
  renderAt('/cases', <CasesPage />)
  const link = (await screen.findByText('Weber ./. Weber')).closest('a')
  expect(link).toHaveAttribute('href', '/cases/ADV-024-A?view=review&open=1')
  expect(within(link as HTMLElement).getByTitle('documents to review')).toHaveTextContent('2')
})
