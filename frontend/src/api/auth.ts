import { useMutation, useQuery } from '@tanstack/react-query'

import { api, type ApiError, type Schemas, unwrap } from './client'

export function useAuthConfig() {
  return useQuery<Schemas['AuthConfig'], ApiError>({
    queryKey: ['auth', 'config'],
    queryFn: () => unwrap(api.GET('/api/v1/auth/config')),
  })
}

export function useLogin() {
  return useMutation<Schemas['SessionStarted'], ApiError, Schemas['LoginRequest']>({
    mutationFn: (body) => unwrap(api.POST('/api/v1/auth/login', { body })),
  })
}

export function useSignup() {
  return useMutation<Schemas['SessionStarted'], ApiError, Schemas['SignupRequest']>({
    mutationFn: (body) => unwrap(api.POST('/api/v1/auth/signup', { body })),
  })
}
