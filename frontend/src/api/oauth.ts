import { useMutation, useQuery } from '@tanstack/react-query'

import { api, type ApiError, type Schemas, unwrap } from './client'

export type AuthorizationRequestParams = {
  client_id: string
  redirect_uri: string
  code_challenge: string
}

export const useAuthorizationRequest = (params: AuthorizationRequestParams) =>
  useQuery<Schemas['AuthorizationRequestView'], ApiError>({
    queryKey: ['oauth', 'authorization-request', params],
    queryFn: () =>
      unwrap(api.GET('/api/v1/oauth/authorization-request', { params: { query: params } })),
    retry: false,
  })

export const useAuthorize = () =>
  useMutation<Schemas['AuthorizeResult'], ApiError, Schemas['AuthorizeDecision']>({
    mutationFn: (body) => unwrap(api.POST('/api/v1/oauth/authorize', { body })),
  })
