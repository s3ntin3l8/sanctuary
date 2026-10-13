import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useLocation } from 'react-router'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../test/render'
import { CommandPalette } from './CommandPalette'

const empty = { documents: [], cases: [], contacts: [], total: 0 }

test('palette exposes Contacts and Settings and resets an empty query', async () => {
  stubApi({ 'GET /api/v1/search': { body: empty } })
  renderAt('/', <CommandPalette open onClose={() => {}} />)
  const dialog = screen.getByRole('dialog', { name: 'Command palette' })
  expect(within(dialog).getByRole('button', { name: 'Contacts' })).toBeVisible()
  expect(within(dialog).getByRole('button', { name: 'Settings' })).toBeVisible()
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Search'), 'Nobody')
  await user.click(await screen.findByRole('button', { name: 'Reset search' }))
  expect(screen.getByLabelText('Search')).toHaveValue('')
  expect(screen.getByLabelText('Search')).toHaveFocus()
  expect(screen.getByRole('button', { name: 'Contacts' })).toBeVisible()
})

test('palette shows request failure with a working retry rather than no results', async () => {
  const fetch = stubApi({ 'GET /api/v1/search': { body: empty } })
  fetch.mockImplementationOnce(async () =>
    Response.json({ detail: 'Unavailable', code: 'unavailable' }, { status: 503 }),
  )
  renderAt('/', <CommandPalette open onClose={() => {}} />)
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Search'), 'Weber')
  expect(await screen.findByRole('alert')).toHaveTextContent('Search failed: Unavailable')
  expect(screen.queryByText(/No results/)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Retry search' }))
  await waitFor(() => expect(screen.getByText(/No results/)).toBeVisible())
})

test('Enter immediately after typing searches the current text instead of activating stale navigation', async () => {
  function Page() {
    const location = useLocation()
    return (
      <>
        <span>{location.pathname + location.search}</span>
        <CommandPalette open onClose={() => {}} />
      </>
    )
  }
  stubApi({ 'GET /api/v1/search': { body: empty } })
  renderAt('/', <Page />)
  await userEvent.setup().type(screen.getByLabelText('Search'), 'Weber{Enter}')
  expect(await screen.findByText('/search?q=Weber')).toBeVisible()
})
