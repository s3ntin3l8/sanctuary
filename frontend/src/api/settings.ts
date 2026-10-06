import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type ApiError, type Schemas, unwrap } from './client'

type S = Schemas

function useSettingsQuery<T>(key: string, fetch: () => Promise<T>) {
  return useQuery<T, ApiError>({ queryKey: ['settings', key], queryFn: fetch })
}

/** Mutation that replaces the cached view with the server's response. */
function useReplace<T, V>(key: string, run: (vars: V) => Promise<T>) {
  const queryClient = useQueryClient()
  return useMutation<T, ApiError, V>({
    mutationFn: run,
    onSuccess: (view) => queryClient.setQueryData(['settings', key], view),
  })
}

// --- Account -----------------------------------------------------------------

export const useAccount = () =>
  useSettingsQuery<S['AccountView']>('account', () => unwrap(api.GET('/api/v1/settings/account')))
export const useUpdateProfile = () =>
  useReplace<S['AccountView'], S['ProfileUpdate']>('account', (body) =>
    unwrap(api.PUT('/api/v1/settings/account/profile', { body })),
  )
export const useChangeEmail = () =>
  useReplace<S['AccountView'], S['EmailChange']>('account', (body) =>
    unwrap(api.PUT('/api/v1/settings/account/email', { body })),
  )
export const useChangePassword = () =>
  useMutation<unknown, ApiError, S['PasswordChange']>({
    mutationFn: (body) => unwrap(api.PUT('/api/v1/settings/account/password', { body })),
  })

// --- Gmail -------------------------------------------------------------------

export const useGmail = () =>
  useSettingsQuery<S['GmailView']>('gmail', () => unwrap(api.GET('/api/v1/settings/gmail')))
export const useSaveGmailFilters = () =>
  useReplace<S['GmailView'], S['GmailFilters']>('gmail', (body) =>
    unwrap(api.PUT('/api/v1/settings/gmail/filters', { body })),
  )
export const useGmailBackfill = () =>
  useMutation<unknown, ApiError, S['GmailBackfill']>({
    mutationFn: (body) => unwrap(api.POST('/api/v1/settings/gmail/backfill', { body })),
  })

// --- Identity ----------------------------------------------------------------

export const useIdentity = () =>
  useSettingsQuery<S['IdentityView']>('identity', () =>
    unwrap(api.GET('/api/v1/settings/identity')),
  )
export const useSaveIdentity = () =>
  useReplace<S['IdentityView'], S['IdentityView']>('identity', (body) =>
    unwrap(api.PUT('/api/v1/settings/identity', { body })),
  )

// --- AI ----------------------------------------------------------------------

export const useAiSettings = () =>
  useSettingsQuery<S['AiSettingsView']>('ai', () => unwrap(api.GET('/api/v1/settings/ai')))

export function useRoleHealth(enabled: boolean) {
  return useQuery<S['RoleHealthView'], ApiError>({
    queryKey: ['settings', 'ai-health'],
    queryFn: () => unwrap(api.GET('/api/v1/settings/ai/health')),
    enabled,
    staleTime: 30_000,
  })
}

export function useInstanceModels(instanceId: string | null) {
  return useQuery<S['ModelsView'], ApiError>({
    queryKey: ['settings', 'ai-models', instanceId],
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/settings/ai/instances/{instance_id}/models', {
          params: { path: { instance_id: instanceId ?? '' } },
        }),
      ),
    enabled: !!instanceId,
    staleTime: 60_000,
  })
}

function useAiInvalidate() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: ['settings', 'ai'] })
    queryClient.invalidateQueries({ queryKey: ['settings', 'ai-health'] })
  }
}

export function useCreateInstance() {
  const invalidate = useAiInvalidate()
  return useMutation<S['AiInstance'], ApiError, S['AiInstanceInput']>({
    mutationFn: (body) => unwrap(api.POST('/api/v1/settings/ai/instances', { body })),
    onSuccess: invalidate,
  })
}

export function useUpdateInstance() {
  const invalidate = useAiInvalidate()
  return useMutation<S['AiInstance'], ApiError, { id: string; body: S['AiInstanceInput'] }>({
    mutationFn: ({ id, body }) =>
      unwrap(
        api.PUT('/api/v1/settings/ai/instances/{instance_id}', {
          params: { path: { instance_id: id } },
          body,
        }),
      ),
    onSuccess: invalidate,
  })
}

export function useDeleteInstance() {
  const invalidate = useAiInvalidate()
  return useMutation<unknown, ApiError, string>({
    mutationFn: (id) =>
      unwrap(
        api.DELETE('/api/v1/settings/ai/instances/{instance_id}', {
          params: { path: { instance_id: id } },
        }),
      ),
    onSuccess: invalidate,
  })
}

export const useTestInstance = () =>
  useMutation<S['AiHealth'], ApiError, string>({
    mutationFn: (id) =>
      unwrap(
        api.POST('/api/v1/settings/ai/instances/{instance_id}/test', {
          params: { path: { instance_id: id } },
        }),
      ),
  })

export function useSetRole() {
  const queryClient = useQueryClient()
  return useMutation<
    S['RoleUpdateResult'],
    ApiError,
    { role: S['AiRole']['role']; body: S['RoleUpdate'] }
  >({
    mutationFn: ({ role, body }) =>
      unwrap(api.PUT('/api/v1/settings/ai/roles/{role}', { params: { path: { role } }, body })),
    onSuccess: (result, { role }) => {
      queryClient.setQueryData<S['AiSettingsView']>(['settings', 'ai'], (prev) =>
        prev
          ? {
              ...prev,
              roles: prev.roles.map((r) => (r.role === role ? result.role : r)),
              embed_index: result.embed_index,
            }
          : prev,
      )
      queryClient.setQueryData<S['RoleHealthView']>(['settings', 'ai-health'], (prev) =>
        prev ? { ...prev, [role]: result.health } : prev,
      )
      queryClient.invalidateQueries({ queryKey: ['settings', 'ai'] })
    },
  })
}

export function useReindex(kind: 'reindex' | 'rebuild-index') {
  const queryClient = useQueryClient()
  return useMutation<S['ReindexJob'], ApiError>({
    mutationFn: () =>
      unwrap(
        kind === 'reindex'
          ? api.POST('/api/v1/settings/ai/reindex')
          : api.POST('/api/v1/settings/ai/rebuild-index'),
      ),
    onSuccess: (job) => queryClient.setQueryData(['settings', 'reindex'], job),
  })
}

export function useReindexStatus(initial: S['ReindexJob'] | null | undefined) {
  return useQuery<S['ReindexJob'] | null, ApiError>({
    queryKey: ['settings', 'reindex'],
    queryFn: () => unwrap(api.GET('/api/v1/settings/ai/reindex/status')),
    initialData: initial === undefined ? undefined : initial,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 4_000 : false),
  })
}

export const useSetEngine = () =>
  useMutation<S['ExtractionEngineUpdate'], ApiError, S['ExtractionEngineUpdate']>({
    mutationFn: (body) => unwrap(api.PUT('/api/v1/settings/ai/extraction-engine', { body })),
  })
export const useSetConcurrency = (kind: 'worker' | 'ocr') =>
  useMutation<S['ConcurrencyResult'], ApiError, S['ConcurrencyUpdate']>({
    mutationFn: (body) =>
      unwrap(
        kind === 'worker'
          ? api.PUT('/api/v1/settings/ai/worker-concurrency', { body })
          : api.PUT('/api/v1/settings/ai/ocr-concurrency', { body }),
      ),
  })
export const useSetDebugRedact = () =>
  useMutation<S['DebugRedactUpdate'], ApiError, S['DebugRedactUpdate']>({
    mutationFn: (body) => unwrap(api.PUT('/api/v1/settings/ai/debug-redact', { body })),
  })

// --- Appearance --------------------------------------------------------------

export const useAppearance = () =>
  useSettingsQuery<S['AppearanceView']>('appearance', () =>
    unwrap(api.GET('/api/v1/settings/appearance')),
  )
export const useSaveTheme = () =>
  useReplace<S['AppearanceView'], S['ThemeUpdate']>('appearance', (body) =>
    unwrap(api.PUT('/api/v1/settings/appearance/theme', { body })),
  )
export const useSaveDashboardCards = () =>
  useReplace<S['AppearanceView'], S['DashboardCards']>('appearance', (body) =>
    unwrap(api.PUT('/api/v1/settings/appearance/dashboard-cards', { body })),
  )
export const useSaveTimezone = () =>
  useReplace<S['AppearanceView'], S['TimezoneUpdate']>('appearance', (body) =>
    unwrap(api.PUT('/api/v1/settings/appearance/timezone', { body })),
  )

// --- Data --------------------------------------------------------------------

export const useDataView = () =>
  useSettingsQuery<S['DataView']>('data', () => unwrap(api.GET('/api/v1/settings/data')))
export function useMaintenance(kind: 'reset-enrichment' | 'clear-all-data') {
  const queryClient = useQueryClient()
  return useMutation<S['MaintenanceResult'], ApiError>({
    mutationFn: () =>
      unwrap(
        kind === 'reset-enrichment'
          ? api.POST('/api/v1/settings/data/reset-enrichment')
          : api.POST('/api/v1/settings/data/clear-all-data'),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['settings', 'data'] }),
  })
}
export function useDebugLogs() {
  return useQuery<S['DebugLogList'], ApiError>({
    queryKey: ['settings', 'debug-logs'],
    queryFn: () => unwrap(api.GET('/api/v1/settings/data/debug-logs')),
  })
}
export function useDebugLog(path: string | null) {
  return useQuery<S['DebugLogView'], ApiError>({
    queryKey: ['settings', 'debug-log', path],
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/settings/data/debug-logs/view', {
          params: { query: { path: path ?? '' } },
        }),
      ),
    enabled: !!path,
  })
}

// --- Admin -------------------------------------------------------------------

export function useAdminUsers() {
  return useQuery<S['AdminUsersView'], ApiError>({
    queryKey: ['admin', 'users'],
    queryFn: () => unwrap(api.GET('/api/v1/admin/users')),
  })
}

function useAdminReplace<V>(run: (vars: V) => Promise<S['AdminUsersView']>) {
  const queryClient = useQueryClient()
  return useMutation<S['AdminUsersView'], ApiError, V>({
    mutationFn: run,
    onSuccess: (view) => queryClient.setQueryData(['admin', 'users'], view),
  })
}

const userPath = (id: number) => ({ params: { path: { user_id: id } } })

export const useAdminCreateUser = () =>
  useAdminReplace<S['AdminUserCreate']>((body) => unwrap(api.POST('/api/v1/admin/users', { body })))
export const useAdminToggleActive = () =>
  useAdminReplace<number>((id) =>
    unwrap(api.PUT('/api/v1/admin/users/{user_id}/active', userPath(id))),
  )
export const useAdminSetRole = () =>
  useAdminReplace<{ id: number; role: S['AdminRoleUpdate']['role'] }>(({ id, role }) =>
    unwrap(api.PUT('/api/v1/admin/users/{user_id}/role', { ...userPath(id), body: { role } })),
  )
export const useAdminDeleteUser = () =>
  useAdminReplace<number>((id) => unwrap(api.DELETE('/api/v1/admin/users/{user_id}', userPath(id))))
export const useAdminReassign = () =>
  useAdminReplace<{ id: number; newOwnerId: number }>(({ id, newOwnerId }) =>
    unwrap(
      api.POST('/api/v1/admin/users/{user_id}/reassign-cases', {
        ...userPath(id),
        body: { new_owner_id: newOwnerId },
      }),
    ),
  )
export const useAdminResetPassword = () =>
  useMutation<unknown, ApiError, { id: number; password: string }>({
    mutationFn: ({ id, password }) =>
      unwrap(
        api.PUT('/api/v1/admin/users/{user_id}/password', {
          ...userPath(id),
          body: { new_password: password },
        }),
      ),
  })
export const useAdminSignup = () =>
  useAdminReplace<boolean>((enabled) =>
    unwrap(api.PUT('/api/v1/admin/signup', { body: { enabled } })),
  )
