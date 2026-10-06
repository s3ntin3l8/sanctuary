import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import * as navigation from '../../navigation'
import { renderAt, stubApi } from '../../test/render'
import { SlicingPage } from './SlicingPage'
import { Route, Routes } from 'react-router'

const view = {
  batch_id: 77,
  subject: 'scan.pdf',
  status: 'ready',
  page_count: 3,
  pages: [1, 2, 3].map((p) => ({
    page: p,
    text_head: `head ${p}`,
    text_tail: `tail ${p}`,
    has_thumbnail: false,
  })),
  proposed_cuts: [{ page: 3, confidence: 'high', notes: 'new letterhead' }],
  error: null,
}

test('starts from the AI proposal, lets the user toggle cuts, and confirms', async () => {
  vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  const fetch = stubApi({
    'GET /api/v1/slicing/77': { body: view },
    'POST /api/v1/slicing/77/confirm': { body: { document_ids: [1, 2, 3] } },
  })
  renderAt(
    '/ingest/slice/77',
    <Routes>
      <Route path="/ingest/slice/:batchId" element={<SlicingPage />} />
    </Routes>,
  )
  const user = userEvent.setup()
  expect(await screen.findByText(/3 pages · 2 documents/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Split after page 2' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await user.click(screen.getByRole('button', { name: 'Split after page 1' }))
  expect(screen.getByText(/3 pages · 3 documents/)).toBeVisible()
  await user.click(screen.getByRole('button', { name: /^Confirm/ }))
  await waitFor(async () => {
    const r = fetch.mock.calls.map(([x]) => x).find((x) => x.method === 'POST')
    expect(await r?.clone().json()).toEqual({ cuts: [1, 2] })
  })
  expect(navigation.leaveTo).toHaveBeenCalledWith('/triage')
})
