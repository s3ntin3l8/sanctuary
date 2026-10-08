import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'

import { Button, buttonClass } from './Button'

test('size sm carries no default-size padding that would out-rank it', () => {
  render(<Button size="sm">Go</Button>)
  const cls = screen.getByRole('button', { name: 'Go' }).className
  expect(cls).toContain('py-1')
  expect(cls).not.toContain('py-2.5')
  expect(cls).not.toContain('px-4')
})

test('danger-outline sets its own text colour exactly once', () => {
  const cls = buttonClass('danger-outline')
  expect(cls).toContain('text-danger')
  expect(cls).not.toContain('text-ink')
})

test('default size is md', () => {
  expect(buttonClass('primary')).toContain('py-1.5')
})
