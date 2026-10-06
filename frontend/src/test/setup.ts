import '@testing-library/jest-dom/vitest'

import { cleanup, configure } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// CI runners are slow and queries resolve through fetch + React Query.
configure({ asyncUtilTimeout: 4000 })

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// jsdom has no layout: stub the observers and scroll APIs the reader uses.
class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return []
  }
}
// Assigned directly (not vi.stubGlobal) so `unstubGlobals` keeps them across tests.
Object.assign(globalThis, { IntersectionObserver: NoopObserver, ResizeObserver: NoopObserver })
Element.prototype.scrollIntoView ??= function scrollIntoView() {}
