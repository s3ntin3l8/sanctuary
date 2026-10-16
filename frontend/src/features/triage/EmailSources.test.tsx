import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../../test/render'
import { EmailSources } from './EmailSources'

const evidence = {
  batch_id: 42,
  received_at: '2026-05-28T15:00:00Z',
  received_source: 'email_date',
  acknowledged: false,
  review_reasons: ['attachment_unsupported'],
  sources: [
    {
      id: '0',
      parent_id: null,
      sender: 'lawyer@example.com',
      subject: 'Outer forwarding',
      to: ['client@example.com'],
      cc: [],
      bcc: [],
      reply_to: [],
      email_note: 'Current context',
      parts: [],
    },
    {
      id: '0.1.m0',
      parent_id: '0',
      sender: 'court@example.com',
      subject: 'Original court transmission',
      to: [],
      cc: [],
      bcc: ['private@example.com'],
      reply_to: [],
      parts: [
        {
          id: '0.1.m0.1',
          filename: 'archive.zip',
          mime_type: 'application/zip',
          outcome: 'unsupported',
          reason: 'File format is not supported',
        },
      ],
    },
  ],
}

test('loads nested transport sources on demand and explains unsupported parts', async () => {
  const fetch = stubApi({ 'GET /api/v1/triage/bundles/42/sources': { body: evidence } })
  renderAt('/triage', <EmailSources batchId={42} />)
  expect(fetch).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Email sources and attachments' }))
  expect(await screen.findByText('Original court transmission')).toBeVisible()
  expect(screen.getByText('Outer forwarding')).toBeVisible()
  expect(screen.getByText(/BCC: private/)).toBeVisible()
  expect(screen.getByText(/arrival unknown/)).toBeVisible()
  expect(screen.getByText('File format is not supported')).toBeVisible()
  expect(screen.getByRole('link', { name: 'Download original' })).toHaveAttribute(
    'href',
    '/api/v1/triage/bundles/42/sources/0.1.m0/parts/0.1.m0.1',
  )
})

test('acknowledges source problems without creating a document', async () => {
  const fetch = stubApi({
    'GET /api/v1/triage/bundles/42/sources': { body: evidence },
    'POST /api/v1/triage/bundles/42/sources/acknowledge': {
      body: { ...evidence, acknowledged: true },
    },
  })
  renderAt('/triage', <EmailSources batchId={42} />)
  await userEvent.click(screen.getByRole('button', { name: 'Email sources and attachments' }))
  await userEvent.click(await screen.findByRole('button', { name: 'Acknowledge source issues' }))
  await waitFor(() =>
    expect(fetch.mock.calls.some(([request]) => request.method === 'POST')).toBe(true),
  )
})

test('labels a migrated receipt without guessing its original date source', async () => {
  stubApi({
    'GET /api/v1/triage/bundles/42/sources': {
      body: {
        ...evidence,
        received_source: 'legacy_unknown',
        review_reasons: ['legacy_source_identity'],
      },
    },
  })
  renderAt('/triage', <EmailSources batchId={42} />)
  await userEvent.click(screen.getByRole('button', { name: 'Email sources and attachments' }))
  expect(await screen.findByText(/Legacy date · origin unknown/)).toBeVisible()
  expect(screen.getByText(/Existing groups are preserved/)).toBeVisible()
})
