import { screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'

import type { Schemas } from '../../api/client'
import { renderAt, stubApi } from '../../test/render'
import { ContactPage } from './ContactPage'

const contact: Schemas['ContactView'] = {
  name: 'RA Müller',
  document_count: 2,
  case_count: 1,
  last_contact: '2026-09-01T00:00:00Z',
  cases: [{ id: 'ADV-024-A', title: 'Vane ./. Vane', status: 'discovery' }],
  documents: [
    {
      id: 1,
      title: 'Klageerwiderung',
      case_id: 'ADV-024-A',
      case_title: 'Vane ./. Vane',
      originator_type: 'opposing',
      issued_date: '2026-09-01T00:00:00Z',
      ingest_date: '2026-09-02T00:00:00Z',
      legal_significance: 'Denies the claim in full.',
    },
    {
      id: 2,
      title: 'Fristverlängerung',
      case_id: 'ADV-024-A',
      case_title: 'Vane ./. Vane',
      originator_type: 'opposing',
      issued_date: null,
      ingest_date: '2026-08-01T00:00:00Z',
      legal_significance: null,
    },
  ],
}

test('shows the correspondent, their cases and what they sent', async () => {
  stubApi({ 'GET /api/v1/contacts': { body: contact } })
  renderAt('/contacts?name=RA%20M%C3%BCller', <ContactPage />)
  expect(await screen.findByRole('heading', { level: 1, name: 'RA Müller' })).toBeVisible()
  expect(
    within(screen.getByRole('list', { name: 'Cases' })).getByText('Vane ./. Vane'),
  ).toBeVisible()
  expect(screen.getByRole('link', { name: /Klageerwiderung/ })).toHaveAttribute(
    'href',
    '/document/1',
  )
  expect(screen.getByText('Denies the claim in full.')).toBeVisible()
  expect(screen.getByRole('link', { name: 'Search all references' })).toHaveAttribute(
    'href',
    '/search?q=RA%20M%C3%BCller',
  )
})

test('asks for a contact when the name is missing', () => {
  stubApi({})
  renderAt('/contacts', <ContactPage />)
  expect(screen.getByText('No contact selected.')).toBeVisible()
})
