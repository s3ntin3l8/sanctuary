import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import type { Schemas } from '../../api/client'
import { caseCard } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { CostsPage } from './CostsPage'

const row: Schemas['CostRow'] = {
  id: 7,
  case_id: 'ADV-024-A',
  proceeding_id: null,
  title: 'Verfahrensgebühr',
  category: 'anwaltskosten',
  status: 'offen',
  rvg_position: 'VV 3100',
  amount_net: 100,
  vat_rate: 0.19,
  amount_gross: 119,
  amount_paid: null,
  amount_reimbursed: null,
  is_reimbursable: true,
  issued_at: '2026-09-01T00:00:00Z',
  due_at: '2026-10-01T00:00:00Z',
  paid_at: null,
  streitwert: 12000,
  gebuehren_faktor: 1.3,
  notes: null,
  auto_created: false,
  source_document_id: null,
}
const overview: Schemas['CostsOverview'] = {
  summary: {
    total_cost_exposure_cents: 0,
    booked: 119,
    paid: 0,
    outstanding: 119,
    reimbursable: 119,
  },
  overdue: [{ cost: row, case_title: 'Vane ./. Vane', open_amount: 119 }],
  due_soon: [],
  cases: [
    {
      id: 'ADV-024-A',
      title: 'Vane ./. Vane',
      status: 'discovery',
      can_edit: true,
      summary: {
        total_cost_exposure_cents: 0,
        booked: 119,
        paid: 0,
        outstanding: 119,
        reimbursable: 119,
      },
      costs: [row],
    },
  ],
}

test('lists the ledger with alerts and books a payment', async () => {
  const fetch = stubApi({
    'GET /api/v1/costs': { body: overview },
    'POST /api/v1/costs/7/pay': { body: { ...row, status: 'bezahlt', amount_paid: 119 } },
  })
  renderAt('/costs', <CostsPage />)
  const user = userEvent.setup()
  expect(await screen.findByRole('heading', { level: 1, name: 'Costs' })).toBeVisible()
  expect(
    within(screen.getByRole('region', { name: 'Overdue' })).getByText('Verfahrensgebühr'),
  ).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'paid' }))
  await waitFor(() =>
    expect(fetch.mock.calls.some(([r]) => r.url.endsWith('/costs/7/pay'))).toBe(true),
  )
})

test('the add-cost form derives gross from net and VAT', async () => {
  const fetch = stubApi({
    'GET /api/v1/costs': { body: { ...overview, cases: [], overdue: [] } },
    'GET /api/v1/cases': { body: { cases: [caseCard], counts_by_status: {}, total: 1 } },
    'POST /api/v1/cases/ADV-024-A/costs': { status: 201, body: row },
  })
  renderAt('/costs', <CostsPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /Add cost/ }))
  const dialog = await screen.findByRole('dialog', { name: 'Add cost' })
  await user.selectOptions(within(dialog).getByLabelText('Case'), 'ADV-024-A')
  await user.type(within(dialog).getByLabelText('Title'), 'Verfahrensgebühr')
  await user.type(within(dialog).getByLabelText('Net amount (€)'), '100')
  expect(within(dialog).getByLabelText('Gross amount')).toHaveValue('119.00')
  await user.click(within(dialog).getByRole('button', { name: 'Book cost' }))
  await waitFor(() => expect(fetch.mock.calls.some(([r]) => r.method === 'POST')).toBe(true))
  const posted = fetch.mock.calls.map(([r]) => r).find((r) => r.method === 'POST')
  expect(await posted?.clone().json()).toMatchObject({
    title: 'Verfahrensgebühr',
    amount_net: 100,
    vat_rate: 0.19,
  })
})
