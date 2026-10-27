import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'

import { RailButton } from './RailButton'

// The badge text is visible, so it has to be part of the accessible name (WCAG 2.5.3).
test('a count is part of the control name and shown on the badge', () => {
  render(<RailButton icon="inbox" label="Triage" count={7} tone="danger" />)
  expect(screen.getByRole('button', { name: 'Triage, 7' })).toBeVisible()
  expect(screen.getByText('7')).toBeVisible()
})

test('large counts read 99+ in both the badge and the name', () => {
  render(<RailButton icon="inbox" label="Triage" count={250} />)
  expect(screen.getByRole('button', { name: 'Triage, 99+' })).toBeVisible()
})

test('no count, no badge, plain name', () => {
  render(<RailButton icon="inbox" label="Triage" count={0} />)
  expect(screen.getByRole('button', { name: 'Triage' })).toBeVisible()
})
