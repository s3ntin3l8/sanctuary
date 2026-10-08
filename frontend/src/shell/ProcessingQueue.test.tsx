import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { emptyQueue } from '../test/fixtures'
import { renderAt, stubApi } from '../test/render'
import { ProcessingQueue } from './ProcessingQueue'

test('queue: says why a document is held back from relationship detection', async () => {
  stubApi({
    'GET /api/v1/worker-queue': {
      body: {
        ...emptyQueue,
        counts: { executing: 0, queued: 2, failed: 0, ai_inflight: 0 },
        queued: [
          {
            kind: 'doc',
            stage: 'relationships',
            label: 'Schriftsatz vom 02.03.',
            doc_id: 12,
            batch_id: 3,
            doc_count: 1,
            note: 'Waiting for earlier documents of the case',
          },
          {
            kind: 'doc',
            stage: 'embeddings',
            label: 'Ladung',
            doc_id: 13,
            batch_id: 4,
            doc_count: 1,
            note: null,
          },
        ],
      },
    },
  })
  renderAt('/', <ProcessingQueue />)

  await userEvent.setup().click(await screen.findByRole('button', { name: 'Processing queue' }))

  expect(await screen.findByText('Waiting for earlier documents of the case')).toBeVisible()
  expect(screen.getAllByText('Waiting for earlier documents of the case')).toHaveLength(1)
})
