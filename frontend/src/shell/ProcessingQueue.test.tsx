import { screen, within } from '@testing-library/react'
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

  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Processing queue' }))
  await user.click(await screen.findByRole('tab', { name: /Queued/ }))

  expect(await screen.findByText('Waiting for earlier documents of the case')).toBeVisible()
  expect(screen.getAllByText('Waiting for earlier documents of the case')).toHaveLength(1)
})

const failedDoc = {
  doc_id: 7,
  batch_id: 2,
  title: 'Klageschrift.pdf',
  stages: [
    { stage: 'enrich', error: 'LLM timeout after 600s', attempt: 3 },
    { stage: 'claims', error: 'upstream enrich failed', attempt: null },
  ],
}

test('queue: opens on Failed when something failed, lists every failed stage and retries the root cause', async () => {
  const fetch = stubApi({
    'GET /api/v1/worker-queue': {
      body: {
        ...emptyQueue,
        counts: { executing: 1, queued: 0, failed: 1, ai_inflight: 1 },
        executing: [
          {
            kind: 'doc',
            stage: 'embeddings',
            label: 'Ladung',
            doc_id: 13,
            batch_id: 4,
            doc_count: 1,
            started_at: new Date(Date.now() - 95_000).toISOString(),
          },
        ],
        failed: [failedDoc],
        ai_calls: ['enrich:doc:7'],
      },
    },
    'POST /api/v1/documents/7/pipeline/enrich/retry': { body: {} },
  })
  renderAt('/', <ProcessingQueue />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Processing queue' }))

  expect(await screen.findByRole('tab', { name: /Failed/, selected: true })).toBeVisible()
  const link = screen.getByRole('link', { name: 'Klageschrift.pdf' })
  expect(link).toHaveAttribute('href', '/document/7')
  expect(screen.getByText('enrich · claims')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Details for D#7' }))
  expect(screen.getByText('LLM timeout after 600s')).toBeVisible()
  expect(screen.getByText('upstream enrich failed')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Retry' }))
  expect(
    fetch.mock.calls.some(
      ([r]) => r.method === 'POST' && r.url.endsWith('/documents/7/pipeline/enrich/retry'),
    ),
  ).toBe(true)

  await user.click(screen.getByRole('tab', { name: /Active/ }))
  expect(screen.getByText(/running 1m 3\ds/)).toBeVisible()
  expect(screen.getByRole('link', { name: 'Ladung' })).toHaveAttribute('href', '/document/13')
  expect(screen.getByText('AI now: enrich:doc:7')).toBeVisible()
})

test('queue: system strip shows workers, backlog and the GPU gate, and tolerates unknowns', async () => {
  stubApi({
    'GET /api/v1/worker-queue': {
      body: {
        ...emptyQueue,
        system: {
          workers: [
            { name: 'ingest', up: true, concurrency: 4 },
            { name: 'ai', up: false, concurrency: 0 },
            { name: 'maintenance', up: true, concurrency: 1 },
          ],
          queue_depth: { ingest: 3, ai: 12, maintenance: 0 },
          gpu_gate: { holders: ['qwen'], waiting: 2 },
        },
      },
    },
  })
  renderAt('/', <ProcessingQueue />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Processing queue' }))
  const strip = await screen.findByLabelText('System status')
  expect(within(strip).getByText('ai down')).toBeVisible()
  expect(within(strip).getByText('backlog 3 ingest · 12 ai · 0 maint')).toBeVisible()
  expect(within(strip).getByText(/GPU qwen · 2 waiting/)).toBeVisible()
  expect(screen.getByText('All document pipelines idle.')).toBeVisible()
})

test('queue: recent tab lists finished stages with their duration', async () => {
  stubApi({
    'GET /api/v1/worker-queue': {
      body: {
        ...emptyQueue,
        system: { workers: null, queue_depth: null, gpu_gate: null },
        recent: [
          {
            doc_id: 5,
            batch_id: 1,
            title: 'Beschluss.pdf',
            stage: 'metadata',
            completed_at: new Date(Date.now() - 5 * 60_000).toISOString(),
            duration_ms: 72_000,
          },
        ],
      },
    },
  })
  renderAt('/', <ProcessingQueue />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Processing queue' }))
  expect(await screen.findByText('Workers: unknown (broker unreachable)')).toBeVisible()
  await user.click(screen.getByRole('tab', { name: /Recent/ }))
  expect(screen.getByRole('link', { name: 'Beschluss.pdf' })).toBeVisible()
  expect(screen.getByText('1m 12s')).toBeVisible()
  expect(screen.getByText('5m')).toBeVisible()
})
