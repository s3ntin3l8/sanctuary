import { Fragment, useDeferredValue, useState } from 'react'
import { Link } from 'react-router'

import { type AiLogFilters, useAiLogs, useAiLogView, useSetDebugRedact } from '../../api/settings'
import type { Schemas } from '../../api/client'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'
import { SettingsCard } from '../../ui/SettingsCard'
import { Toggle } from '../../ui/Toggle'

type Row = Schemas['DebugLogRow']

const FIELD =
  'rounded-lg border border-line bg-card2 px-2 py-1 text-[11.5px] text-ink outline-none focus:border-accent'

function formatDuration(ms: number | null): string {
  if (ms == null) return ''
  return ms < 10_000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms / 1000)}s`
}

export function AiLogsPage() {
  const [kind, setKind] = useState('')
  const [stage, setStage] = useState('')
  const [model, setModel] = useState('')
  const [status, setStatus] = useState<'' | 'ok' | 'error'>('')
  const [docId, setDocId] = useState('')
  const [batchId, setBatchId] = useState('')
  const [text, setText] = useState('')
  const q = useDeferredValue(text.trim())
  const filters: AiLogFilters = {
    kind: kind || undefined,
    stage: stage || undefined,
    model: model || undefined,
    status: status || undefined,
    doc_id: /^\d+$/.test(docId) ? Number(docId) : undefined,
    batch_id: /^\d+$/.test(batchId) ? Number(batchId) : undefined,
    q: q || undefined,
  }
  const query = useAiLogs(filters)
  const logs = query.data
  const [open, setOpen] = useState<string | null>(null)
  const redact = useSetDebugRedact()
  const [redactOn, setRedactOn] = useState<boolean | null>(null)

  if (!logs) return <QueryState error={query.error} pending={query.isPending} />
  const redactValue = redactOn ?? logs.ai_debug_redact
  const filtered = Object.values(filters).some((v) => v !== undefined)

  return (
    <>
      <SettingsCard title="Log privacy">
        <div className="flex items-center gap-3 text-[12px]">
          <div className="flex-1">
            <div className="font-semibold">Redact PII in AI debug logs</div>
            <div className="text-muted">
              Masks names and addresses in the prompts written to the local debug log.
            </div>
          </div>
          <Toggle
            checked={redactValue}
            label="Redact PII in AI debug logs"
            disabled={redact.isPending}
            onChange={(next) => {
              setRedactOn(next)
              redact.mutate({ enabled: next }, { onError: () => setRedactOn(!next) })
            }}
          />
        </div>
      </SettingsCard>

      <SettingsCard title="AI call log" description={`Local debug index in ${logs.log_root}`}>
        <div className="flex flex-wrap items-center gap-2" role="search" aria-label="Filter calls">
          <select
            aria-label="Kind"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            className={FIELD}
          >
            <option value="">All kinds</option>
            {logs.facets.kind.map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
          <select
            aria-label="Stage"
            value={stage}
            onChange={(e) => setStage(e.target.value)}
            className={`${FIELD} max-w-44`}
          >
            <option value="">All stages</option>
            {logs.facets.stage.map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
          <select
            aria-label="Model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className={`${FIELD} max-w-44`}
          >
            <option value="">All models</option>
            {logs.facets.model.map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
          <select
            aria-label="Status"
            value={status}
            onChange={(e) => setStatus(e.target.value as '' | 'ok' | 'error')}
            className={FIELD}
          >
            <option value="">Any status</option>
            <option value="ok">OK</option>
            <option value="error">Errors only</option>
          </select>
          <input
            aria-label="Document id"
            placeholder="Doc #"
            inputMode="numeric"
            value={docId}
            onChange={(e) => setDocId(e.target.value)}
            className={`${FIELD} w-20 font-mono`}
          />
          <input
            aria-label="Bundle id"
            placeholder="Bundle #"
            inputMode="numeric"
            value={batchId}
            onChange={(e) => setBatchId(e.target.value)}
            className={`${FIELD} w-24 font-mono`}
          />
          <input
            aria-label="Search"
            placeholder="Search scope or error"
            value={text}
            onChange={(e) => setText(e.target.value)}
            className={`${FIELD} min-w-40 flex-1`}
          />
          {filtered && (
            <button
              type="button"
              onClick={() => {
                setKind('')
                setStage('')
                setModel('')
                setStatus('')
                setDocId('')
                setBatchId('')
                setText('')
              }}
              className="text-[11px] text-muted hover:underline"
            >
              Clear
            </button>
          )}
        </div>

        {logs.rows.length === 0 ? (
          <p className="text-[12px] text-muted">
            {filtered ? 'No calls match these filters.' : 'No AI calls recorded yet.'}
          </p>
        ) : (
          <table className="w-full table-fixed text-[11px]">
            <thead className="text-left text-[9.5px] font-extrabold tracking-[.1em] text-muted uppercase">
              <tr>
                <th className="w-36 py-1 pr-3">Time</th>
                <th className="w-14 pr-3">Kind</th>
                <th className="w-24 pr-3">Scope</th>
                <th className="w-40 pr-3">Stage</th>
                <th className="pr-3">Model</th>
                <th className="w-16 pr-3 text-right">Duration</th>
                <th className="w-16 pl-3">Status</th>
                <th className="w-6" />
              </tr>
            </thead>
            <tbody className="font-mono">
              {logs.rows.map((r, i) => {
                const key = `${r.ts}-${i}`
                return (
                  <Fragment key={key}>
                    <tr
                      className={`border-t border-line2 ${r.path ? 'cursor-pointer hover:bg-accent/5' : ''}`}
                      onClick={() => r.path && setOpen(open === key ? null : key)}
                    >
                      <td className="py-1 pr-3 text-muted">
                        {r.ts?.slice(0, 19).replace('T', ' ')}
                      </td>
                      <td className="pr-3">{r.kind}</td>
                      <td className="truncate pr-3">{r.scope_id}</td>
                      <td className="truncate pr-3">{r.stage}</td>
                      <td className="truncate pr-3">{r.model}</td>
                      <td className="pr-3 text-right">{formatDuration(r.duration_ms)}</td>
                      <td
                        className={`pl-3 ${r.status === 'ok' ? 'text-success' : r.status ? 'text-danger' : ''}`}
                      >
                        {r.status}
                      </td>
                      <td className="text-right text-muted">
                        {r.path && (
                          <button
                            type="button"
                            aria-label={open === key ? 'Hide call' : 'View call'}
                            aria-expanded={open === key}
                            onClick={(e) => {
                              e.stopPropagation()
                              setOpen(open === key ? null : key)
                            }}
                          >
                            <Icon name={open === key ? 'expand_less' : 'expand_more'} size={16} />
                          </button>
                        )}
                      </td>
                    </tr>
                    {open === key && (
                      <tr>
                        <td colSpan={8} className="pb-2">
                          <CallDetail row={r} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        )}
      </SettingsCard>
    </>
  )
}

function CallDetail({ row }: { row: Row }) {
  const log = useAiLogView(row).data
  const meta = [
    row.provider && `provider ${row.provider}`,
    row.ttfb_ms != null && `ttfb ${formatDuration(row.ttfb_ms)}`,
    row.prompt_tokens != null && `${row.prompt_tokens} prompt tok`,
    row.completion_tokens != null && `${row.completion_tokens} completion tok`,
    row.reasoning_tokens != null && `${row.reasoning_tokens} reasoning tok`,
    row.watchdog && `watchdog: ${row.watchdog}`,
  ].filter(Boolean)
  return (
    <div className="rounded-xl border border-line bg-card2 p-3 font-sans">
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted">
        {meta.map((m) => (
          <span key={String(m)}>{m}</span>
        ))}
        {row.doc_id != null && (
          <Link to={`/document/${row.doc_id}`} className="text-tealink hover:underline">
            Document #{row.doc_id}
          </Link>
        )}
        {row.batch_id != null && (
          <Link
            to={`/triage?bundle=batch-${row.batch_id}`}
            className="text-tealink hover:underline"
          >
            Bundle #{row.batch_id}
          </Link>
        )}
      </div>
      {row.error && (
        <p className="mb-2 rounded-lg bg-danger/10 px-2 py-1 font-mono text-[11px] text-danger">
          {row.error}
        </p>
      )}
      <pre className="max-h-96 overflow-auto font-mono text-[11px] whitespace-pre-wrap">
        {log?.body ?? 'Loading…'}
      </pre>
    </div>
  )
}
