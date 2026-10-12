import { screen } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { expect, test } from 'vitest'

import { conversation, documentReader } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { DocumentPage } from './DocumentPage'

function page(reader: typeof documentReader) {
  stubApi({
    'GET /api/v1/documents/2211/reader': { body: reader },
    'POST /api/v1/chat/conversations': { body: conversation },
    'GET /api/v1/chat/conversations/31': { body: conversation },
    'GET /api/v1/chat/conversations': { body: [] },
  })
  renderAt(
    '/document/2211',
    <Routes>
      <Route path="/document/:id" element={<DocumentPage />} />
    </Routes>,
  )
}

const AGENT = 'Claude Code'

test('marks the reaction, pin, relationship and action an agent wrote', async () => {
  const [pin] = documentReader.pins
  const [relationship] = documentReader.relationships
  const [action] = documentReader.actions
  if (!pin || !relationship || !action) throw new Error('fixture lacks pin/relationship/action')
  page({
    ...documentReader,
    pins: [{ ...pin, agent_token_name: AGENT }],
    relationships: [{ ...relationship, agent_token_name: AGENT }],
    actions: [{ ...action, agent_token_name: AGENT }],
    reactions: [{ reaction: 'lies', notes: null, created_at: null, agent_token_name: AGENT }],
  })
  await screen.findByRole('heading', { name: 'Klageerwiderung.pdf' })
  expect(await screen.findAllByText(`via agent · ${AGENT}`)).toHaveLength(4)
})

test('shows no agent badge for the user’s own rows', async () => {
  page(documentReader)
  await screen.findByRole('heading', { name: 'Klageerwiderung.pdf' })
  expect(screen.queryByText(/via agent/)).not.toBeInTheDocument()
})
