import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router'

import { ApiError } from './api/client'
import { LoginPage } from './features/auth/LoginPage'
import { SignupPage } from './features/auth/SignupPage'
import { CasesPage } from './features/cases/CasesPage'
import { HomePage } from './features/home/HomePage'
import { leaveTo } from './navigation'
import { Shell } from './shell/Shell'

// Every path here must also be served by FastAPI as an SPA route (app/spa.py).
const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  { path: '/signup', element: <SignupPage /> },
  {
    element: <Shell />,
    children: [
      { path: '/', element: <HomePage /> },
      { path: '/cases', element: <CasesPage /> },
    ],
  },
])

function makeQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        // The session expired or was revoked: back to sign-in, then return here.
        if (error instanceof ApiError && error.status === 401) {
          const here = window.location.pathname + window.location.search
          leaveTo(`/login?next=${encodeURIComponent(here)}`)
        }
      },
    }),
  })
}

export function App() {
  const [queryClient] = useState(makeQueryClient)
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
}
