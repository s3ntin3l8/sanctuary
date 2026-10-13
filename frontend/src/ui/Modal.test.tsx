import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState, useEffect, useRef } from 'react'
import { expect, test } from 'vitest'

import { Drawer } from './Drawer'
import { Modal } from './Modal'

function Dialogs() {
  const [open, setOpen] = useState(false)
  const [nested, setNested] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Open form</button>
      <Modal open={open} onClose={() => setOpen(false)} title="Form">
        <input aria-label="Name" />
        <button onClick={() => setNested(true)}>More details</button>
        <Modal open={nested} onClose={() => setNested(false)} title="Nested form">
          <input aria-label="Detail" />
        </Modal>
      </Modal>
    </>
  )
}

test('dialog contains focus in both directions and restores the opener', async () => {
  render(<Dialogs />)
  const user = userEvent.setup()
  const opener = screen.getByRole('button', { name: 'Open form' })
  await user.click(opener)
  expect(screen.getByLabelText('Name')).toHaveFocus()
  await user.tab()
  expect(screen.getByRole('button', { name: 'More details' })).toHaveFocus()
  await user.tab()
  expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()
  await user.tab({ shift: true })
  expect(screen.getByRole('button', { name: 'More details' })).toHaveFocus()
  opener.focus()
  expect(screen.getByLabelText('Name')).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(opener).toHaveFocus()
})

test('Escape closes only the topmost dialog and resumes the underlying form', async () => {
  render(<Dialogs />)
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Open form' }))
  await user.click(screen.getByRole('button', { name: 'More details' }))
  expect(screen.getByLabelText('Detail')).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog', { name: 'Nested form' })).not.toBeInTheDocument()
  expect(screen.getByRole('dialog', { name: 'Form' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'More details' })).toHaveFocus()
})

test('turning a drawer into an inline pane preserves an unsaved draft', async () => {
  const props = { open: true, onClose: () => {}, title: 'Chat', header: false }
  const { rerender } = render(
    <Drawer {...props}>
      <textarea aria-label="Draft" />
    </Drawer>,
  )
  const user = userEvent.setup()
  await user.type(within(screen.getByRole('dialog')).getByLabelText('Draft'), 'Keep this draft')
  const textarea = screen.getByLabelText('Draft')
  rerender(
    <Drawer {...props} inline>
      <textarea aria-label="Draft" />
    </Drawer>,
  )
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Draft')).toBe(textarea)
  expect(textarea).toHaveValue('Keep this draft')
  rerender(
    <Drawer {...props}>
      <textarea aria-label="Draft" />
    </Drawer>,
  )
  expect(within(screen.getByRole('dialog')).getByLabelText('Draft')).toBe(textarea)
})

test('a child that autofocuses after mounting does not replace the drawer opener', async () => {
  function Composer() {
    const input = useRef<HTMLTextAreaElement>(null)
    useEffect(() => {
      input.current?.focus()
    }, [])
    return <textarea ref={input} aria-label="Composer" />
  }
  function Chat() {
    const [open, setOpen] = useState(false)
    return (
      <>
        <button onClick={() => setOpen(true)}>Ask about passage</button>
        <Drawer open={open} onClose={() => setOpen(false)} title="Chat">
          <Composer />
        </Drawer>
      </>
    )
  }
  render(<Chat />)
  const user = userEvent.setup()
  const opener = screen.getByRole('button', { name: 'Ask about passage' })
  await user.click(opener)
  expect(screen.getByLabelText('Composer')).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(opener).toHaveFocus()
})
