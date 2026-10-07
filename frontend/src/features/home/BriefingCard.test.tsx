import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import type { Schemas } from '../../api/client'
import { renderAt, stubApi } from '../../test/render'
import { BriefingCard } from './BriefingCard'

const ready: Schemas['BriefingView'] = {
  status: 'ready',
  day: '2026-06-17',
  generated_at: '2026-06-17T06:40:00Z',
  model_label: 'qwen3.5:9b',
  external: false,
  summary: 'Two deadlines land this week. ADV-024-A is due tomorrow.',
  priorities: ['ADV-024-A: file the counter-statement', 'Triage: 2 bundles'],
  error: null,
}

test('ready: summary, priorities and the local-model label', async () => {
  stubApi({ 'GET /api/v1/home/briefing': { body: ready } })
  renderAt('/', <BriefingCard />)
  const card = await screen.findByRole('region', { name: 'Morning briefing' })
  expect(card).toHaveTextContent('ADV-024-A is due tomorrow')
  expect(card).toHaveTextContent('generated locally')
  expect(card).toHaveTextContent('qwen3.5:9b')
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
})

test('processing shows the wait line and disables refresh', async () => {
  stubApi({
    'GET /api/v1/home/briefing': {
      body: { ...ready, status: 'processing', summary: null, priorities: [], generated_at: null },
    },
    'POST /api/v1/home/briefing/refresh': {
      status: 202,
      body: { ...ready, status: 'processing', summary: null, priorities: [] },
    },
  })
  renderAt('/', <BriefingCard />)
  expect(await screen.findByText(/Reading today's deadlines/)).toBeVisible()
  expect(screen.getByRole('region', { name: 'Morning briefing' })).toHaveAttribute(
    'aria-busy',
    'true',
  )
  expect(screen.getByRole('button', { name: 'Regenerate briefing' })).toBeDisabled()
})

test('failed shows the error and refresh re-arms', async () => {
  const fetch = stubApi({
    'GET /api/v1/home/briefing': {
      body: { ...ready, status: 'failed', summary: null, priorities: [], error: 'provider down' },
    },
    'POST /api/v1/home/briefing/refresh': {
      status: 202,
      body: { ...ready, status: 'processing', summary: null, priorities: [], error: null },
    },
  })
  renderAt('/', <BriefingCard />)
  expect(await screen.findByRole('alert')).toHaveTextContent('provider down')
  await userEvent.click(screen.getByRole('button', { name: 'Regenerate briefing' }))
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(([r]) => r.method === 'POST' && r.url.endsWith('/briefing/refresh')),
    ).toBe(true),
  )
  expect(await screen.findByText(/Reading today's deadlines/)).toBeVisible()
})

test('an external endpoint is named, never "local"', async () => {
  stubApi({ 'GET /api/v1/home/briefing': { body: { ...ready, external: true } } })
  renderAt('/', <BriefingCard />)
  const card = await screen.findByRole('region', { name: 'Morning briefing' })
  expect(card).toHaveTextContent('generated on an external endpoint')
  expect(card).not.toHaveTextContent('generated locally')
})

test('a rejected refresh shows the API error', async () => {
  stubApi({
    'GET /api/v1/home/briefing': { body: ready },
    'POST /api/v1/home/briefing/refresh': {
      status: 429,
      body: { detail: 'Rate limit exceeded: 6 per 1 hour', code: 'rate_limited' },
    },
  })
  renderAt('/', <BriefingCard />)
  await screen.findByRole('region', { name: 'Morning briefing' })
  await userEvent.click(screen.getByRole('button', { name: 'Regenerate briefing' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Rate limit exceeded')
})
