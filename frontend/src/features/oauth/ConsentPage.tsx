import { useState } from 'react'
import { useSearchParams } from 'react-router'

import { useCasesDirectory } from '../../api/cases'
import { useAuthorize, useAuthorizationRequest } from '../../api/oauth'
import { leaveTo } from '../../navigation'
import { Alert } from '../../ui/Alert'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { AuthLayout } from '../auth/AuthLayout'

/**
 * Where an MCP client (Claude Desktop, claude.ai, …) asks to read this
 * Sanctuary. The server's /authorize endpoint sends the browser here; approving
 * returns the browser to the client with a one-time code carrying the scope
 * chosen below.
 */
export function ConsentPage() {
  const [params] = useSearchParams()
  const request = {
    client_id: params.get('client_id') ?? '',
    redirect_uri: params.get('redirect_uri') ?? '',
    code_challenge: params.get('code_challenge') ?? '',
  }
  const info = useAuthorizationRequest(request)
  const cases = useCasesDirectory().data?.cases ?? []
  const authorize = useAuthorize()
  const [allCases, setAllCases] = useState(true)
  const [picked, setPicked] = useState<string[]>([])
  const [access, setAccess] = useState<'read' | 'read_write'>('read')

  function decide(decision: 'approve' | 'deny') {
    authorize.mutate(
      {
        ...request,
        redirect_uri_provided_explicitly: params.get('explicit') !== '0',
        state: params.get('state'),
        resource: params.get('resource'),
        decision,
        access,
        all_cases: allCases,
        case_ids: allCases ? [] : picked,
      },
      { onSuccess: (result) => leaveTo(result.redirect_url) },
    )
  }

  if (info.error) {
    return (
      <AuthLayout subtitle="Connect an agent">
        <Alert>
          This authorization request is not valid. Start the connection again from the app.
        </Alert>
      </AuthLayout>
    )
  }
  if (!info.data) return <AuthLayout subtitle="Connect an agent">Loading…</AuthLayout>
  const { client_name, redirect_host, mcp_enabled } = info.data

  return (
    <AuthLayout subtitle="Connect an agent" error={authorize.error?.message}>
      <div className="space-y-4">
        <p className="text-[13px]">
          <strong>{client_name}</strong> wants to read your cases
          <span className="block font-mono text-[11px] text-muted">
            then return to {redirect_host}
          </span>
        </p>
        <p className="flex gap-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-[11.5px] text-ink2">
          <Icon name="warning" size={16} />
          <span>
            Case content is sent to whatever model this app uses. If that is a cloud model, it
            leaves this device.
          </span>
        </p>

        {!mcp_enabled ? (
          <Alert>An admin has switched agent access off for this instance.</Alert>
        ) : (
          <>
            <fieldset className="space-y-2">
              <legend className="text-[10px] font-bold tracking-[.1em] text-muted uppercase">
                Cases
              </legend>
              <label className="flex items-center gap-2 text-[12.5px]">
                <input type="radio" checked={allCases} onChange={() => setAllCases(true)} />
                All cases, including ones created later
              </label>
              <label className="flex items-center gap-2 text-[12.5px]">
                <input type="radio" checked={!allCases} onChange={() => setAllCases(false)} />
                Only these cases
              </label>
              {!allCases && (
                <ul className="max-h-40 space-y-1 overflow-y-auto rounded-lg border border-line p-2">
                  {cases.map((c) => (
                    <li key={c.id}>
                      <label className="flex items-center gap-2 text-[12px]">
                        <input
                          type="checkbox"
                          checked={picked.includes(c.id)}
                          onChange={(e) =>
                            setPicked((p) =>
                              e.target.checked ? [...p, c.id] : p.filter((x) => x !== c.id),
                            )
                          }
                        />
                        <span className="font-mono text-[11px]">{c.id}</span>
                        <span className="truncate text-muted">{c.title}</span>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </fieldset>

            <fieldset className="space-y-2">
              <legend className="text-[10px] font-bold tracking-[.1em] text-muted uppercase">
                Access
              </legend>
              <label className="flex items-center gap-2 text-[12.5px]">
                <input
                  type="radio"
                  checked={access === 'read'}
                  onChange={() => setAccess('read')}
                />
                Read-only
              </label>
              <label className="flex items-center gap-2 text-[12.5px]">
                <input
                  type="radio"
                  checked={access === 'read_write'}
                  onChange={() => setAccess('read_write')}
                />
                Read &amp; write — annotate, add claims and deadlines, upload
              </label>
            </fieldset>
          </>
        )}

        <div className="flex gap-2">
          {mcp_enabled && (
            <Button
              className="flex-1"
              disabled={authorize.isPending || (!allCases && picked.length === 0)}
              onClick={() => decide('approve')}
            >
              Allow
            </Button>
          )}
          <Button
            className="flex-1"
            variant="secondary"
            disabled={authorize.isPending}
            onClick={() => decide('deny')}
          >
            Deny
          </Button>
        </div>
        <p className="text-center text-[10.5px] text-muted">
          You can revoke this any time in Settings → Agent access.
        </p>
      </div>
    </AuthLayout>
  )
}
