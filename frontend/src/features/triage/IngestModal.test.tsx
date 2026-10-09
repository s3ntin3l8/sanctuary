import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import { renderAt, stubApi } from '../../test/render'

import { IngestModal } from './IngestModal'

const pdf = () => new File(['%PDF'], 'stack.pdf', { type: 'application/pdf' })

test('split toggle only appears for triage uploads with a PDF queued', async () => {
  stubApi({})
  const user = userEvent.setup()
  renderAt('/triage', <IngestModal open onClose={vi.fn()} caseId={null} />)
  const toggle = /scanned stack/i
  expect(screen.queryByLabelText(toggle)).toBeNull()
  await user.upload(screen.getByLabelText('Choose files'), pdf())
  expect(await screen.findByLabelText(toggle)).not.toBeChecked()
})

test('hides the split toggle when uploading into a case', async () => {
  stubApi({
    'GET /api/v1/upload/target': {
      body: { case_id: 'ADV-1', case_title: 'T', parent_options: [] },
    },
  })
  const user = userEvent.setup()
  renderAt('/cases/ADV-1', <IngestModal open onClose={vi.fn()} caseId="ADV-1" />)
  await user.upload(screen.getByLabelText('Choose files'), pdf())
  await screen.findByText('stack.pdf')
  expect(screen.queryByLabelText(/scanned stack/i)).toBeNull()
})

test('sends split_scans and links the result to the slicing review', async () => {
  stubApi({})
  // useUpload calls fetch(url, init) directly rather than going through openapi-fetch.
  const fetch = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(async () =>
    Response.json({
      results: [{ filename: 'stack.pdf', status: 'queued', batch_id: 91, slicing: true }],
      queued: 1,
      failed: 0,
      batch_id: null,
    }),
  )
  vi.stubGlobal('fetch', fetch)
  const user = userEvent.setup()
  renderAt('/triage', <IngestModal open onClose={vi.fn()} caseId={null} />)
  await user.upload(screen.getByLabelText('Choose files'), pdf())
  await user.click(await screen.findByLabelText(/scanned stack/i))
  await user.click(screen.getByRole('button', { name: /^Ingest/ }))
  const link = await screen.findByRole('link', { name: 'Review cuts' })
  expect(link).toHaveAttribute('href', '/ingest/slice/91')
  const body = fetch.mock.calls[0]?.[1].body as FormData
  expect(body?.get('split_scans')).toBe('true')
})
