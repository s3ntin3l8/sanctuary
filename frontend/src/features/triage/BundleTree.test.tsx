import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { expect, test } from 'vitest'

import type { Schemas } from '../../api/client'
import { triageBundle } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'

import { BundleTree } from './BundleTree'

/** A DataTransfer stand-in — jsdom has none, the handlers only write to it. */
function dataTransfer() {
  return {
    setData: () => {},
    effectAllowed: '',
    dropEffect: '',
  } as unknown as DataTransfer
}

function renderTree() {
  renderAt('/triage', <BundleTree bundle={triageBundle} activeDocId={null} onSelect={() => {}} />)
}

test('the cover letter leads its group and cannot be dragged away', () => {
  renderTree()
  const rows = screen.getAllByRole('listitem')
  // Begleitschreiben.pdf (2210) is the cover letter, the two others its enclosures.
  expect(rows).toHaveLength(3)
  expect(rows[0]).toHaveAttribute('draggable', 'false')
  expect(rows[1]).toHaveAttribute('draggable', 'true')
  expect(rows[2]).toHaveAttribute('draggable', 'true')
  // The heading does not advertise a drag it cannot do.
  expect(within(rows[0] as HTMLElement).queryByText('drag_indicator')).toBeNull()
  expect(within(rows[1] as HTMLElement).getByText('drag_indicator')).toBeVisible()
})

test('the cover letter cannot be moved into another group', () => {
  const bundle: Schemas['TriageBundle'] = {
    ...triageBundle,
    has_manual_groups: true,
    sub_groups: [
      {
        id: 'batch-42-g0',
        sub_group_id: 1,
        label: 'Klageerwiderung.pdf',
        lead_doc_id: 2210,
        suggested_case_id: null,
        suggested_case_title: null,
        case_confidence: null,
        doc_ids: [2210, 2211, 2212],
      },
      {
        id: 'batch-42-g1',
        sub_group_id: 2,
        label: 'Anlagen',
        lead_doc_id: null,
        suggested_case_id: null,
        suggested_case_title: null,
        case_confidence: null,
        doc_ids: [],
      },
    ],
  }
  renderAt('/triage', <BundleTree bundle={bundle} activeDocId={null} onSelect={() => {}} />)
  const rows = screen.getAllByRole('listitem')
  expect(within(rows[0] as HTMLElement).queryByLabelText(/^Move /)).toBeNull()
  expect(within(rows[1] as HTMLElement).getByLabelText(/^Move /)).toBeVisible()
})

test('dropping a document never puts it above the cover letter', async () => {
  const fetch = stubApi({
    'PUT /api/v1/triage/bundles/42/groups/0/order': { body: triageBundle },
  })
  renderTree()
  const rows = screen.getAllByRole('listitem')

  // Drag the second document onto the cover letter.
  fireEvent.dragStart(rows[1] as HTMLElement, { dataTransfer: dataTransfer() })
  fireEvent.drop(rows[0] as HTMLElement, { dataTransfer: dataTransfer() })

  await waitFor(async () => {
    const call = fetch.mock.calls.find(([r]) => r.method === 'PUT')
    expect(call).toBeTruthy()
    const body = (await (call?.[0] as Request).clone().json()) as { doc_ids: number[] }
    expect(body.doc_ids).toEqual([2210, 2211, 2212])
  })
})
