import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'

import { AgentBadge } from './AgentBadge'

test('names the agent that wrote the row', () => {
  render(<AgentBadge name="Claude Code" />)
  expect(screen.getByText(/via agent · Claude Code/)).toBeVisible()
})

test.each([null, undefined, ''])('renders nothing for the user’s own row (%s)', (name) => {
  const { container } = render(<AgentBadge name={name} />)
  expect(container).toBeEmptyDOMElement()
})
