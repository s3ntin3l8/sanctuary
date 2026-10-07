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
