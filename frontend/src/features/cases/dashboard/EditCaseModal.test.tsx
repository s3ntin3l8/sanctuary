import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import type { Schemas } from '../../../api/client'
import { caseDetail } from '../../../test/fixtures'
import { renderAt, stubApi } from '../../../test/render'
import { EditCaseModal } from './EditCaseModal'

const [first] = caseDetail.proceedings as [Schemas['ProceedingView']]
const second: Schemas['ProceedingView'] = {
  ...first,
  id: 11,
  court_name: 'LG Hamburg',
  az_court: '9 T 2/26',
}
const detail: Schemas['CaseDetail'] = {
  ...caseDetail,
  proceedings: [...caseDetail.proceedings, second],
}

test('merges a proceeding into another of the case', async () => {
  const fetch = stubApi({
    'POST /api/v1/proceedings/11/merge': { body: first },
  })
  renderAt('/', <EditCaseModal open onClose={() => {}} detail={detail} />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /Merge proceeding LG Hamburg/ }))
  const dialog = await screen.findByRole('dialog', { name: /Merge proceeding 9 T 2\/26/ })
  const merge = within(dialog).getByRole('button', { name: 'Merge' })
  expect(merge).toBeDisabled()
  await user.selectOptions(within(dialog).getByRole('combobox'), String(first.id))
  await user.click(merge)
  await waitFor(async () => {
    const call = fetch.mock.calls
      .map(([x]) => x)
      .find((x) => x.method === 'POST' && x.url.endsWith('/proceedings/11/merge'))
    expect(await call?.clone().json()).toEqual({ target_id: first.id })
  })
})
