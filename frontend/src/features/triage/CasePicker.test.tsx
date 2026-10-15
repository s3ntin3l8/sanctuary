import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import { CasePicker } from './CasePicker'

const cases = [
  { id: '8441-25', title: 'Teilungsversteigerung', is_draft: false },
  { id: '8245-25', title: 'Zugewinnausgleich', is_draft: true },
] as never

test('typing an unknown ID offers one-step creation with a prefilled title', async () => {
  const onCreate = vi.fn()
  render(
    <CasePicker
      cases={cases}
      defaultTitle="Nutzungsentschädigung"
      onPick={vi.fn()}
      onCreate={onCreate}
    />,
  )
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Filter cases'), '8250-25')
  await user.click(screen.getByRole('button', { name: /Create case/ }))
  expect(screen.getByLabelText('New case ID')).toHaveValue('8250-25')
  expect(screen.getByLabelText('New case title')).toHaveValue('Nutzungsentschädigung')
  await user.click(screen.getByRole('button', { name: /Create & assign/ }))
  expect(onCreate).toHaveBeenCalledWith({ id: '8250-25', title: 'Nutzungsentschädigung' })
})

test('an exact existing ID offers no create row', async () => {
  render(<CasePicker cases={cases} onPick={vi.fn()} onCreate={vi.fn()} />)
  await userEvent.setup().type(screen.getByLabelText('Filter cases'), '8441-25')
  expect(screen.queryByRole('button', { name: /Create case/ })).not.toBeInTheDocument()
})
