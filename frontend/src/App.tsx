import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router'

import { ApiError } from './api/client'
import { AdminUsersPage } from './features/admin/AdminUsersPage'
import { LoginPage } from './features/auth/LoginPage'
import { SignupPage } from './features/auth/SignupPage'
import { CasesPage } from './features/cases/CasesPage'
import { CasePage } from './features/cases/dashboard/CasePage'
import { ContactPage } from './features/contacts/ContactPage'
import { CostsPage } from './features/costs/CostsPage'
import { DocumentPage } from './features/documents/DocumentPage'
import { ImportPage } from './features/import/ImportPage'
import { HomePage } from './features/home/HomePage'
import { NotFoundPage } from './features/NotFoundPage'
import { AccountPage } from './features/settings/AccountPage'
import { AiPage } from './features/settings/AiPage'
import { AppearancePage } from './features/settings/AppearancePage'
import { DataPage } from './features/settings/DataPage'
import { ExportPage } from './features/settings/ExportPage'
import { GmailPage } from './features/settings/GmailPage'
import { IdentityPage } from './features/settings/IdentityPage'
import { SettingsLayout } from './features/settings/SettingsLayout'
import { SearchPage } from './features/search/SearchPage'
import { SlicingPage } from './features/slicing/SlicingPage'
import { TriagePage } from './features/triage/TriagePage'
import { leaveTo } from './navigation'
import { Shell } from './shell/Shell'
import { ShortcutsProvider } from './shell/shortcuts'
import { ToastProvider } from './ui/toast'

// Every path here must also be served by FastAPI as an SPA route (app/api/spa_pages.py).
const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  { path: '/signup', element: <SignupPage /> },
  {
    element: <Shell />,
    children: [
      { path: '/', element: <HomePage /> },
      { path: '/cases', element: <CasesPage /> },
      { path: '/cases/:caseId', element: <CasePage /> },
      { path: '/triage', element: <TriagePage /> },
      { path: '/ingest/slice/:batchId', element: <SlicingPage /> },
      { path: '/document/:id', element: <DocumentPage /> },
      { path: '/import', element: <ImportPage /> },
      { path: '/costs', element: <CostsPage /> },
      { path: '/contacts', element: <ContactPage /> },
      { path: '/search', element: <SearchPage /> },
      {
        element: <SettingsLayout />,
        children: [
          { path: '/settings', element: <Navigate to="/settings/account" replace /> },
          { path: '/settings/account', element: <AccountPage /> },
          { path: '/settings/appearance', element: <AppearancePage /> },
          { path: '/settings/ai', element: <AiPage /> },
          { path: '/settings/identity', element: <IdentityPage /> },
          { path: '/settings/gmail', element: <GmailPage /> },
          { path: '/settings/data', element: <DataPage /> },
          { path: '/settings/export', element: <ExportPage /> },
          { path: '/admin/users', element: <AdminUsersPage /> },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])

/** The session expired or was revoked: back to sign-in, then return here. */
function redirectOnUnauthorized(error: unknown) {
  if (error instanceof ApiError && error.status === 401) {
    const here = window.location.pathname + window.location.search
    leaveTo(`/login?next=${encodeURIComponent(here)}`)
  }
}

function makeQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({ onError: redirectOnUnauthorized }),
    mutationCache: new MutationCache({ onError: redirectOnUnauthorized }),
  })
}

export function App() {
  const [queryClient] = useState(makeQueryClient)
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <ShortcutsProvider>
          <RouterProvider router={router} />
        </ShortcutsProvider>
      </ToastProvider>
    </QueryClientProvider>
  )
}
