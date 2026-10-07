import { screen } from '@testing-library/react'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../test/render'
import { NotFoundPage } from './NotFoundPage'

test('names the missing path and links home', () => {
  stubApi({})
  renderAt('/no/such/page', <NotFoundPage />)
  expect(screen.getByRole('heading', { level: 1, name: 'Nothing here' })).toBeVisible()
  expect(screen.getByText('/no/such/page')).toBeVisible()
  expect(screen.getByRole('link', { name: 'Back to Home' })).toHaveAttribute('href', '/')
})
