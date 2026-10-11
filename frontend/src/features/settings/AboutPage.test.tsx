import { screen } from '@testing-library/react'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../../test/render'
import { AboutPage } from './AboutPage'

test('about shows the running build', async () => {
  stubApi({
    'GET /api/v1/about': {
      body: {
        version: 'v0.3.0',
        git_sha: '371f165ab',
        build_date: '2026-10-11T10:00:00Z',
        db_revision: 'a1b2c3',
      },
    },
  })
  renderAt('/settings/about', <AboutPage />)
  expect(await screen.findByText('v0.3.0')).toBeVisible()
  expect(screen.getByText('371f165ab')).toBeVisible()
  expect(screen.getByText('2026-10-11T10:00:00Z')).toBeVisible()
  expect(screen.getByText('a1b2c3')).toBeVisible()
})
