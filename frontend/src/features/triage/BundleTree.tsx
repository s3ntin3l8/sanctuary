import { type FormEvent, type ReactNode, useState } from 'react'

import { type TriageBundle, useGroupOp } from '../../api/triage'
import { formatShortDate } from '../../format'
import { Badge } from '../../ui/Badge'
import { Icon } from '../../ui/Icon'
import { useToast } from '../../ui/toast'
import { ORIGINATOR_COLOR } from '../documents/DocumentReview'

type Props = {
  bundle: TriageBundle
  activeDocId: number | null
  onSelect: (id: number) => void
  /** Rendered under the source/received lines (the bundle-level confirm). */
  footer?: ReactNode
}

/** Bundle contents: sub-groups (cover letter + enclosures) with manual grouping controls. */
export function BundleTree({ bundle, activeDocId, onSelect, footer }: Props) {
  const op = useGroupOp()
  const toast = useToast()
  const docsById = new Map(bundle.documents.map((d) => [d.id, d]))
  const batchId = bundle.batch_id
  const run = (vars: Parameters<typeof op.mutate>[0]) =>
    op.mutate(vars, { onError: (e) => toast(e.message, 'error') })

  return (
    <div className="flex flex-col">
      <div className="mb-2 flex items-center gap-2">
        <Icon name="account_tree" size={14} className="text-accent" />
        <h4 className="text-[9.5px] font-bold tracking-[.1em] text-muted uppercase">
          Bundle contents · {bundle.doc_count}
        </h4>
        {batchId !== null && (
          <span className="ml-auto flex gap-1">
            <button
              type="button"
              title="New group"
              aria-label="New group"
              onClick={() => run({ batchId, op: 'new' })}
              className="text-muted hover:text-ink"
            >
              <Icon name="create_new_folder" size={15} />
            </button>
            {bundle.has_manual_groups && (
              <button
                type="button"
                title="Reset to automatic grouping"
                aria-label="Reset groups"
                onClick={() => run({ batchId, op: 'reset' })}
                className="text-muted hover:text-ink"
              >
                <Icon name="restart_alt" size={15} />
              </button>
            )}
          </span>
        )}
      </div>
      <div className="space-y-2">
        {bundle.sub_groups.map((g) => (
          <div key={g.id} className="rounded-lg border border-line2">
            <GroupHeader
              group={g}
              batchId={batchId}
              manual={bundle.has_manual_groups}
              onRename={(label) =>
                g.sub_group_id !== null &&
                batchId !== null &&
                run({ batchId, op: 'rename', subGroupId: g.sub_group_id, label })
              }
              onDelete={() =>
                g.sub_group_id !== null &&
                batchId !== null &&
                run({ batchId, op: 'delete', subGroupId: g.sub_group_id })
              }
            />
            <ul>
              {g.doc_ids.map((id) => {
                const d = docsById.get(id)
                if (!d) return null
                const otherGroups = bundle.sub_groups.filter(
                  (x) => x.sub_group_id !== null && x.id !== g.id,
                )
                return (
                  <li
                    key={id}
                    className={`flex items-center gap-1 pr-2 ${activeDocId === id ? 'bg-accent/10' : 'hover:bg-accent/5'}`}
                  >
                    <button
                      type="button"
                      onClick={() => onSelect(id)}
                      aria-current={activeDocId === id ? 'true' : undefined}
                      className="flex min-w-0 flex-1 items-center gap-2 py-1.5 text-left text-[11.5px]"
                      style={{ paddingLeft: 8 + d.depth * 12 }}
                    >
                      <span
                        className={`h-1.5 w-1.5 shrink-0 rounded-full ${ORIGINATOR_COLOR[d.originator_type]}`}
                      />
                      <Icon
                        name={
                          d.role === 'cover_letter'
                            ? 'description'
                            : d.is_proof
                              ? 'verified'
                              : 'attach_file'
                        }
                        size={14}
                        className="text-muted"
                      />
                      <span className="min-w-0 flex-1 truncate">{d.title}</span>
                      {d.pipeline_state === 'failed' && <Badge tone="danger">failed</Badge>}
                      {d.needs_review && d.pipeline_state === 'completed' && (
                        <span
                          className="h-1.5 w-1.5 rounded-full bg-warning"
                          title="needs review"
                        />
                      )}
                      <span className="font-mono text-[10px] text-muted">{d.page_count} pp</span>
                    </button>
                    {batchId !== null && bundle.has_manual_groups && otherGroups.length > 0 && (
                      <select
                        aria-label={`Move ${d.title} to group`}
                        value=""
                        onChange={(e) => {
                          const target = otherGroups.find(
                            (x) => String(x.sub_group_id) === e.target.value,
                          )
                          if (target && target.sub_group_id !== null)
                            run({
                              batchId,
                              op: 'order',
                              subGroupId: target.sub_group_id,
                              docIds: [...target.doc_ids, id],
                            })
                        }}
                        className="w-5 cursor-pointer bg-transparent text-[10px] text-muted"
                        title="Move to group"
                      >
                        <option value="">⇄</option>
                        {otherGroups.map((x) => (
                          <option key={x.id} value={String(x.sub_group_id)}>
                            → {x.label}
                          </option>
                        ))}
                      </select>
                    )}
                    {batchId !== null && d.role !== 'cover_letter' && (
                      <button
                        type="button"
                        title="Mark as cover letter"
                        aria-label={`Mark ${d.title} as cover letter`}
                        onClick={() => run({ batchId, op: 'cover', docId: id })}
                        className="text-muted hover:text-ink"
                      >
                        <Icon name="bookmark" size={13} />
                      </button>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </div>
      <div className="mt-2 space-y-0.5 font-mono text-[10px] text-muted">
        <div>source · {bundle.sender_email ?? bundle.source_type}</div>
        <div>received · {formatShortDate(bundle.received_at)}</div>
      </div>
      {footer}
    </div>
  )
}

function GroupHeader({
  group,
  batchId,
  manual,
  onRename,
  onDelete,
}: {
  group: TriageBundle['sub_groups'][number]
  batchId: number | null
  manual: boolean
  onRename: (label: string) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = useState(false)
  const canEdit = manual && batchId !== null && group.sub_group_id !== null
  return (
    <div className="flex items-center gap-2 border-b border-line2 bg-panel2 px-2 py-1">
      {editing ? (
        <form
          className="flex-1"
          onSubmit={(e: FormEvent<HTMLFormElement>) => {
            e.preventDefault()
            onRename(String(new FormData(e.currentTarget).get('label')))
            setEditing(false)
          }}
        >
          <input
            name="label"
            defaultValue={group.label}
            autoFocus
            aria-label="Group label"
            className="w-full bg-transparent text-[11px] outline-none"
            onBlur={(e) => e.currentTarget.form?.requestSubmit()}
          />
        </form>
      ) : (
        <button
          type="button"
          disabled={!canEdit}
          onClick={() => setEditing(true)}
          className="min-w-0 flex-1 truncate text-left text-[11px] font-semibold disabled:cursor-default"
        >
          {group.label}
        </button>
      )}
      {group.suggested_case_id && (
        <span className="font-mono text-[10px] text-tealink">{group.suggested_case_id}</span>
      )}
      {canEdit && (
        <button
          type="button"
          aria-label={`Delete group ${group.label}`}
          onClick={onDelete}
          className="text-muted hover:text-danger"
        >
          <Icon name="delete" size={13} />
        </button>
      )}
    </div>
  )
}
