import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { expect, test } from 'vitest'

import { Modal } from '../ui/Modal'
import { ROW_ATTR, useRovingRows } from './keys'
import { ShortcutsProvider, usePageShortcuts } from './shortcuts'

const PAGE_KEYS = [['x', 'Do the thing']] as const

function Page() {
  usePageShortcuts('Test page', PAGE_KEYS)
  useRovingRows()
  const [open, setOpen] = useState(false)
  return (
    <>
      <a href="#a" {...{ [ROW_ATTR]: '' }}>
        Row A
      </a>
      <a href="#b" {...{ [ROW_ATTR]: '' }}>
        Row B
      </a>
      <button type="button" onClick={() => setOpen(true)}>
        Open modal
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title="Some modal">
        <input aria-label="Field" />
      </Modal>
    </>
  )
}

test('pages register their section once, ? toggles the sheet, j wraps forward', async () => {
  const user = userEvent.setup()
  const { unmount } = (await import('@testing-library/react')).render(
    <ShortcutsProvider>
      <Page />
    </ShortcutsProvider>,
  )
  await user.keyboard('?')
  const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
  expect(within(dialog).getAllByRole('region', { name: 'Test page' })).toHaveLength(1)
  expect(within(dialog).getByText('Do the thing')).toBeVisible()
  expect(within(dialog).getByRole('region', { name: 'Everywhere' })).toBeVisible()
  await user.keyboard('?')
  await waitFor(() => expect(dialog).not.toBeInTheDocument())

  await user.keyboard('j')
  expect(document.activeElement).toHaveTextContent('Row A')
  await user.keyboard('j')
  expect(document.activeElement).toHaveTextContent('Row B')
  await user.keyboard('j')
  expect(document.activeElement).toHaveTextContent('Row A')

  unmount()
})

test('while a modal is open, ? does not stack and j/k do not move; focus returns on close', async () => {
  const user = userEvent.setup()
  ;(await import('@testing-library/react')).render(
    <ShortcutsProvider>
      <Page />
    </ShortcutsProvider>,
  )
  await user.keyboard('j')
  const rowA = document.activeElement
  expect(rowA).toHaveTextContent('Row A')

  await user.click(screen.getByRole('button', { name: 'Open modal' }))
  const modal = screen.getByRole('dialog', { name: 'Some modal' })
  // The modal focused its field; blur it so the key reaches the window.
  ;(document.activeElement as HTMLElement).blur()
  await user.keyboard('?')
  expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument()
  await user.keyboard('j')
  expect(document.activeElement).toBe(document.body)

  await user.keyboard('{Escape}')
  await waitFor(() => expect(modal).not.toBeInTheDocument())
  // Focus goes back to what opened the modal (the button), not to body.
  expect(document.activeElement).toHaveTextContent('Open modal')
})
