import '@testing-library/jest-dom/vitest'

import { cleanup, configure } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// CI runners are slow and queries resolve through fetch + React Query.
configure({ asyncUtilTimeout: 4000 })

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
