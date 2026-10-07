import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { notificationsView } from '../test/fixtures'
import { renderAt, stubApi } from '../test/render'
import { NotificationsPanel } from './NotificationsPanel'

test('badge shows the total and the popover lists each non-empty group with links', async () => {
  stubApi({ 'GET /api/v1/notifications': { body: notificationsView } })
  renderAt('/', <NotificationsPanel />)
  const user = userEvent.setup()
  const bell = await screen.findByRole('button', { name: 'Notifications' })
  expect(await within(bell).findByText('4')).toBeVisible()

  await user.click(bell)
  const panel = screen.getByRole('dialog', { name: 'Notifications' })
  expect(within(panel).getByRole('region', { name: 'Overdue' })).toBeVisible()
  expect(within(panel).queryByRole('region', { name: 'Due this week' })).not.toBeInTheDocument()
  expect(within(panel).getByRole('link', { name: /Stellungnahme Jugendamt/ })).toHaveAttribute(
    'href',
    '/cases/ADV-024-A?view=review',
  )
  expect(within(panel).getByRole('link', { name: /Ladung AG Hamburg/ })).toHaveAttribute(
    'href',
    '/triage',
  )
  expect(within(panel).getByRole('link', { name: /Gerichtskostenvorschuss/ })).toHaveAttribute(
    'href',
    '/costs',
  )
  expect(within(panel).getByText('Saal 3', { exact: false })).toBeVisible()

  await user.click(within(panel).getByRole('link', { name: /Ladung AG Hamburg/ }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

test('shows the "+N more" line when a group is capped', async () => {
  const overdue = notificationsView.groups[0]
  if (!overdue) throw new Error('fixture')
  stubApi({
    'GET /api/v1/notifications': {
      body: { ...notificationsView, total: 8, groups: [{ ...overdue, count: 8 }] },
    },
  })
  renderAt('/', <NotificationsPanel />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Notifications' }))
  expect(screen.getByText('+7 more')).toBeVisible()
})

test('empty state when nothing is due and Escape closes', async () => {
  stubApi({
    'GET /api/v1/notifications': {
      body: { total: 0, generated_at: '2026-06-17T06:40:00Z', groups: [] },
    },
  })
  renderAt('/', <NotificationsPanel />)
  const user = userEvent.setup()
  const bell = await screen.findByRole('button', { name: 'Notifications' })
  expect(within(bell).queryByText('0')).not.toBeInTheDocument()
  await user.click(bell)
  expect(screen.getByText('Nothing needs you right now.')).toBeVisible()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
