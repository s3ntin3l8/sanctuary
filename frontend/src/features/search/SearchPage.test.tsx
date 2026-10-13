import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import type { Schemas } from '../../api/client'
import { renderAt, stubApi } from '../../test/render'
import { SearchPage } from './SearchPage'

const results: Schemas['SearchResults'] = {
  documents: [{ id: 1, title: 'Weber letter', case_id: 'ADV-024-A' }],
  cases: [{ id: 'ADV-024-A', title: 'Weber ./. Weber', status: 'discovery' }],
  contacts: [{ name: 'RA Weber' }],
  total: 2,
}

test('lists every result kind and links contacts by name', async () => {
  const fetch = stubApi({ 'GET /api/v1/search': { body: results } })
  renderAt('/search?q=Weber', <SearchPage />)
  expect(await screen.findByText('3 results for “Weber”')).toBeVisible()
  const url = new URL(fetch.mock.calls.map(([r]) => r.url)[0] ?? '')
  expect(url.searchParams.get('q')).toBe('Weber')
  expect(url.searchParams.get('limit')).toBe('120')
  expect(within(screen.getByRole('heading', { name: /Documents/ })).getByText('1')).toBeVisible()
  expect(screen.getByRole('link', { name: /RA Weber/ })).toHaveAttribute(
    'href',
    '/contacts?name=RA%20Weber',
  )
})

test('submitting the box updates the query', async () => {
  const fetch = stubApi({ 'GET /api/v1/search': { body: results } })
  renderAt('/search', <SearchPage />)
  const user = userEvent.setup()
  expect(screen.getByText('Type at least two characters.')).toBeVisible()
  await user.type(screen.getByLabelText('Search'), 'Weber{Enter}')
  await waitFor(() => expect(fetch).toHaveBeenCalled())
  const url = new URL(fetch.mock.calls.map(([r]) => r.url)[0] ?? '')
  expect(url.searchParams.get('q')).toBe('Weber')
})

test('an empty search can be reset without leaving the previous query in the input', async () => {
  stubApi({ 'GET /api/v1/search': { body: { documents: [], cases: [], contacts: [], total: 0 } } })
  renderAt('/search?q=Nobody', <SearchPage />)
  expect(await screen.findByText(/No results for/)).toBeVisible()
  await userEvent.setup().click(screen.getByRole('button', { name: 'Reset search' }))
  expect(screen.getByLabelText('Search')).toHaveValue('')
  expect(screen.getByText('Type at least two characters.')).toBeVisible()
})

test('a failed search is reported separately from no matches and can be retried', async () => {
  const fetch = stubApi({ 'GET /api/v1/search': { body: results } })
  fetch.mockImplementationOnce(async () =>
    Response.json({ detail: 'Search unavailable', code: 'unavailable' }, { status: 503 }),
  )
  renderAt('/search?q=Weber', <SearchPage />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Search unavailable')
  expect(screen.queryByText(/No results for/)).not.toBeInTheDocument()
  await userEvent.setup().click(screen.getByRole('button', { name: 'Retry search' }))
  expect(await screen.findByText('3 results for “Weber”')).toBeVisible()
})
