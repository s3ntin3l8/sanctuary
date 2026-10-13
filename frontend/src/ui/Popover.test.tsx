import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { expect, test } from 'vitest'

import { Popover } from './Popover'

function Harness() {
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState(false)
  return (
    <nav style={{ overflow: 'auto', width: 56 }}>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        label="Account"
        role="menu"
        anchor={<button onClick={() => setOpen(true)}>Profile</button>}
      >
        <button role="menuitem" onClick={() => setSelected(true)}>
          Settings
        </button>
        {selected && <span>Selected</span>}
      </Popover>
    </nav>
  )
}

test('rail popovers escape the scroll container and allow their controls to be activated', async () => {
  render(<Harness />)
  const user = userEvent.setup()
  const trigger = screen.getByRole('button', { name: 'Profile' })
  await user.click(trigger)
  const menu = screen.getByRole('menu')
  expect(menu.parentElement).toBe(document.body)
  expect(menu.closest('nav')).toBeNull()
  await user.click(screen.getByRole('menuitem', { name: 'Settings' }))
  expect(screen.getByText('Selected')).toBeVisible()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
})
