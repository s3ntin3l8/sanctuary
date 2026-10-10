import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { documentReview } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { ReviewChecklist } from './ReviewChecklist'

const flagged = {
  ...documentReview,
  review_reasons: ['ocr_unverified'],
  pipeline: {
    ...documentReview.pipeline,
    ocr_unverified: [
      { page: 2, words: ['wohnplatz', 'sicherung'] },
      { page: 5, words: ['muniba', ...Array.from({ length: 10 }, (_, i) => `wort${i}`)] },
    ],
  },
}

test('lists the pages whose OCR text could not be corroborated and lets the user mark them checked', async () => {
  const fetch = stubApi({
    'POST /api/v1/documents/1/ocr-unverified/acknowledge': {
      body: {
        ...flagged,
        review_reasons: [],
        pipeline: { ...flagged.pipeline, ocr_unverified: [] },
      },
    },
  })
  renderAt('/triage', <ReviewChecklist review={{ ...flagged, id: 1 }} />)
  const user = userEvent.setup()

  expect(screen.getByText('OCR text to check on pages 2, 5')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Details' }))
  expect(screen.getByText('wohnplatz, sicherung')).toBeVisible()
  expect(screen.getByText(/^muniba, wort0.*\+3 more$/)).toBeVisible()

  await user.click(screen.getByRole('button', { name: 'Checked' }))
  await waitFor(() => {
    const sent = fetch.mock.calls.map(([r]) => `${r.method} ${new URL(r.url).pathname}`)
    expect(sent).toContain('POST /api/v1/documents/1/ocr-unverified/acknowledge')
  })
})

test('shows no OCR row once the cross-check found nothing', () => {
  stubApi({})
  renderAt('/triage', <ReviewChecklist review={{ ...documentReview, id: 1, review_reasons: [] }} />)
  expect(screen.queryByText(/OCR text to check/)).not.toBeInTheDocument()
})
