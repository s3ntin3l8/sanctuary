import createClient from 'openapi-fetch'

import type { components, paths } from './schema'

export type Schemas = components['schemas']

/** A non-2xx `/api/v1` response: `code` is stable, `detail` is safe to show. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

export const api = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: 'same-origin',
  // Looked up per call (not captured here) so tests can stub the global.
  fetch: (request) => globalThis.fetch(request),
})

type Result<T> = { data?: T; error?: unknown; response: Response }

function isErrorBody(value: unknown): value is Schemas['ErrorResponse'] {
  if (typeof value !== 'object' || value === null) return false
  const body = value as Record<string, unknown>
  return typeof body.detail === 'string' && typeof body.code === 'string'
}

/** Unwrap an `api` call: the body on success, a thrown {@link ApiError} otherwise. */
export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  let result: Result<T>
  try {
    result = await call
  } catch {
    throw new ApiError(0, 'network_error', 'Could not reach the server.')
  }
  if (result.data !== undefined) return result.data
  const { error, response } = result
  if (isErrorBody(error)) throw new ApiError(response.status, error.code, error.detail)
  throw new ApiError(response.status, 'error', 'Something went wrong. Please try again.')
}
