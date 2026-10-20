import { type FormEvent, type ReactNode, useState } from 'react'

import { type TriageBundle, useGroupOp } from '../../api/triage'
import { formatShortDate } from '../../time'
import { Badge } from '../../ui/Badge'
import { Icon } from '../../ui/Icon'
import { useToast } from '../../ui/toast'
import { ORIGINATOR_COLOR } from '../documents/DocumentReview'
import { EmailSources } from './EmailSources'

type Props = {
  bundle: TriageBundle
  activeDocId: number | null
  onSelect: (id: number) => void
  /** Archive or delete one document; the caller confirms. Omitted for single-doc bundles. */
  onDocAction?: (doc: TriageBundle['documents'][number], action: 'dismiss' | 'delete') => void
  /** Rendered under the source/received lines (the bundle-level confirm). */
  footer?: ReactNode
}

/** Bundle contents: sub-groups (cover letter + enclosures) with manual grouping controls. */
export function BundleTree({ bundle, activeDocId, onSelect, onDocAction, footer }: Props) {
  const op = useGroupOp()
  const toast = useToast()
  const docsById = new Map(bundle.documents.map((d) => [d.id, d]))
  const batchId = bundle.batch_id
  const run = (vars: Parameters<typeof op.mutate>[0]) =>
    op.mutate(vars, { onError: (e) => toast(e.message, 'error') })

  const [draggedDocId, setDraggedDocId] = useState<number | null>(null)
  const [dropTargetGroupId, setDropTargetGroupId] = useState<string | null>(null)

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
        {bundle.sub_groups.map((g) => {
          // The heading — the group's cover letter — always leads the list and
          // cannot be dragged: it is what the group hangs on.
          const headingId = g.doc_ids.find((id) => {
            const d = docsById.get(id)
            return d?.role === 'cover_letter' && d.depth === 0
          })
          return (
            <div
              key={g.id}
              onDragOver={(e) => {
                if (draggedDocId !== null && batchId !== null) {
                  e.preventDefault()
                  e.dataTransfer.dropEffect = 'move'
                  if (dropTargetGroupId !== g.id) {
                    setDropTargetGroupId(g.id)
                  }
                }
              }}
              onDragLeave={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget as Node)) {
                  if (dropTargetGroupId === g.id) setDropTargetGroupId(null)
                }
              }}
              onDrop={(e) => {
                e.preventDefault()
                setDropTargetGroupId(null)
                if (draggedDocId === null || batchId === null) return
                const docId = draggedDocId
                if (!g.doc_ids.includes(docId)) {
                  run({
                    batchId,
                    op: 'order',
                    subGroupId: g.sub_group_id ?? 0,
                    docIds: [...g.doc_ids, docId],
                    leadDocId: g.lead_doc_id,
                  })
                }
                setDraggedDocId(null)
              }}
              className={`rounded-lg border transition-all ${
                dropTargetGroupId === g.id &&
                draggedDocId !== null &&
                !g.doc_ids.includes(draggedDocId)
                  ? 'border-accent ring-2 ring-accent/40 bg-accent/5'
                  : 'border-line2'
              }`}
            >
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
                  const isHeading = id === headingId
                  const otherGroups = bundle.sub_groups.filter(
                    (x) => x.sub_group_id !== null && x.id !== g.id,
                  )
                  return (
                    <li
                      key={id}
                      draggable={batchId !== null && !isHeading}
                      onDragStart={(e) => {
                        setDraggedDocId(id)
                        e.dataTransfer.setData('text/plain', String(id))
                        e.dataTransfer.effectAllowed = 'move'
                      }}
                      onDragEnd={() => {
                        setDraggedDocId(null)
                        setDropTargetGroupId(null)
                      }}
                      onDragOver={(e) => {
                        if (
                          draggedDocId !== null &&
                          draggedDocId !== id &&
                          g.doc_ids.includes(draggedDocId)
                        ) {
                          e.preventDefault()
                          e.stopPropagation()
                          e.dataTransfer.dropEffect = 'move'
                        }
                      }}
                      onDrop={(e) => {
                        if (
                          draggedDocId !== null &&
                          draggedDocId !== id &&
                          batchId !== null &&
                          g.doc_ids.includes(draggedDocId)
                        ) {
                          e.preventDefault()
                          e.stopPropagation()
                          const reordered = g.doc_ids.filter((x) => x !== draggedDocId)
                          const targetIdx = reordered.indexOf(id)
                          // Nothing lands above the heading — it leads the group.
                          const headingIdx =
                            headingId === undefined ? -1 : reordered.indexOf(headingId)
                          const insertAt =
                            headingIdx >= 0 ? Math.max(targetIdx, headingIdx + 1) : targetIdx
                          reordered.splice(insertAt, 0, draggedDocId)
                          run({
                            batchId,
                            op: 'order',
                            subGroupId: g.sub_group_id ?? 0,
                            docIds: reordered,
                            leadDocId: g.lead_doc_id,
                          })
                          setDraggedDocId(null)
                          setDropTargetGroupId(null)
                        }
                      }}
                      className={`group/doc flex items-center gap-1 pr-2 transition-opacity ${
                        draggedDocId === id ? 'opacity-40' : ''
                      } ${activeDocId === id ? 'bg-accent/10' : 'hover:bg-accent/5'}`}
                    >
                      <button
                        type="button"
                        onClick={() => onSelect(id)}
                        aria-current={activeDocId === id ? 'true' : undefined}
                        className={`flex min-w-0 flex-1 items-center gap-2 py-1.5 text-left text-[11.5px] ${
                          isHeading ? 'cursor-default' : 'cursor-grab active:cursor-grabbing'
                        }`}
                        style={{ paddingLeft: 8 + d.depth * 12 }}
                      >
                        {batchId !== null && (
                          <Icon
                            name="drag_indicator"
                            size={12}
                            className="shrink-0 text-muted/30 group-hover/doc:text-muted"
                          />
                        )}
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
                        {d.exhibit_label && (
                          <span className="shrink-0 rounded-sm bg-accent/10 px-1 font-mono text-[10px] text-muted">
                            {d.exhibit_label}
                          </span>
                        )}
                        <span className="min-w-0 flex-1 truncate">{d.title}</span>
                        {d.reference_inherited && (
                          <span title="Case taken over from its bundle">
                            <Icon name="link" size={12} className="text-muted2" />
                          </span>
                        )}
                        {d.pipeline_state === 'failed' && <Badge tone="danger">failed</Badge>}
                        {d.needs_review && d.pipeline_state === 'completed' && (
                          <span
                            className="h-1.5 w-1.5 rounded-full bg-warning"
                            title="needs review"
                          />
                        )}
                        <span className="font-mono text-[10px] text-muted">{d.page_count} pp</span>
                      </button>
                      {batchId !== null &&
                        bundle.has_manual_groups &&
                        otherGroups.length > 0 &&
                        !isHeading && (
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
                      {onDocAction && bundle.doc_count > 1 && (
                        <span className="flex gap-1 opacity-0 group-focus-within/doc:opacity-100 group-hover/doc:opacity-100">
                          <button
                            type="button"
                            title="Archive document"
                            aria-label={`Archive ${d.title}`}
                            onClick={() => onDocAction(d, 'dismiss')}
                            className="text-muted hover:text-ink"
                          >
                            <Icon name="archive" size={13} />
                          </button>
                          <button
                            type="button"
                            title="Delete document"
                            aria-label={`Delete ${d.title}`}
                            onClick={() => onDocAction(d, 'delete')}
                            className="text-muted hover:text-danger"
                          >
                            <Icon name="delete" size={13} />
                          </button>
                        </span>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          )
        })}
      </div>
      <div className="mt-2 space-y-0.5 font-mono text-[10px] text-muted">
        <div>source · {bundle.sender_email ?? bundle.source_type}</div>
        <div>
          {bundle.received_source === 'email_date'
            ? 'sent · arrival unknown'
            : bundle.received_source === 'ingest_time'
              ? 'ingested · arrival unknown'
              : bundle.received_source === 'legacy_unknown'
                ? 'legacy date · origin unknown'
                : 'received'}{' '}
          · {formatShortDate(bundle.received_at)}
        </div>
      </div>
      {bundle.batch_id != null && (bundle.email_source_count ?? 0) > 0 && (
        <EmailSources batchId={bundle.batch_id} />
      )}
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
