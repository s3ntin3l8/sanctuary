import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef, useState } from 'react'
import { expect, test } from 'vitest'

import { Menu } from './Menu'

function Harness() {
  const [open, setOpen] = useState(false)
  const anchor = useRef<HTMLButtonElement>(null)
  return (
    <ul style={{ opacity: 0.9 }}>
      <li>
        <button ref={anchor} onClick={() => setOpen((o) => !o)}>
          trigger
        </button>
        <Menu open={open} onClose={() => setOpen(false)} anchorRef={anchor} label="Row actions">
          <button role="menuitem">Archive</button>
        </Menu>
      </li>
    </ul>
  )
}

test('renders into document.body, outside the trapped row, and closes on Escape', async () => {
  render(<Harness />)
  const user = userEvent.setup()
  expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'trigger' }))
  const menu = screen.getByRole('menu', { name: 'Row actions' })
  expect(menu.closest('ul')).toBeNull()
  expect(menu.parentElement).toBe(document.body)
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('menu')).not.toBeInTheDocument()
})

test('closes on outside click', async () => {
  render(<Harness />)
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'trigger' }))
  await user.click(screen.getByRole('button', { name: 'Close menu' }))
  expect(screen.queryByRole('menu')).not.toBeInTheDocument()
})
