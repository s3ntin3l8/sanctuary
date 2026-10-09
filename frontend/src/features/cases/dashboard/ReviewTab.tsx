import { useState } from 'react'

import { useOpenDocument } from '../../documents/useOpenDocument'

import type { CaseDetail } from '../../../api/caseDetail'
import { formatShortDate } from '../../../format'
import { Badge } from '../../../ui/Badge'
import { Icon } from '../../../ui/Icon'
import { DocumentReview, ORIGINATOR_COLOR } from '../../documents/DocumentReview'

type Props = {
  detail: CaseDetail
  /** Start with the "to review" filter on (Home card link). */
  initialOnlyOpen?: boolean
  selectedDoc: number | null
  onSelect: (id: number) => void
}

/** The case spine (documents of the active proceeding) beside the selected document's review. */
export function ReviewTab({ detail, selectedDoc, onSelect, initialOnlyOpen = false }: Props) {
  const openDocument = useOpenDocument()
  const [onlyOpen, setOnlyOpen] = useState(initialOnlyOpen)
  const openCount = detail.documents.filter((d) => d.needs_review).length
  const docs = onlyOpen ? detail.documents.filter((d) => d.needs_review) : detail.documents
  return (
    <div className="flex min-h-0 flex-1">
      <aside
        aria-label="Case spine"
        className="flex w-[280px] shrink-0 flex-col border-r border-line bg-bg"
      >
        <h2 className="px-4 pt-4 pb-2 text-[10px] font-bold tracking-[.14em] text-muted uppercase">
          Case spine <span className="font-mono normal-case">{detail.documents.length}</span>
          {detail.new_doc_count > 0 && (
            <Badge tone="accent" className="ml-2">
              {detail.new_doc_count} new
            </Badge>
          )}
          {openCount > 0 && (
            <button
              type="button"
              aria-pressed={onlyOpen}
              aria-label={onlyOpen ? 'Show all documents' : 'Show only documents to review'}
              onClick={() => setOnlyOpen((v) => !v)}
              className="ml-2 align-middle"
            >
              <Badge tone={onlyOpen ? 'warning' : 'neutral'} pill>
                {openCount} to review
              </Badge>
            </button>
          )}
        </h2>
        <ul className="flex-1 overflow-y-auto px-3 pb-3">
          {docs.map((d) => (
            <li key={d.id}>
              <button
                type="button"
                onClick={() => onSelect(d.id)}
                aria-current={selectedDoc === d.id ? 'true' : undefined}
                className={`relative my-0.5 w-full rounded-lg py-2 pr-2 pl-6 text-left text-[12px] ${selectedDoc === d.id ? 'bg-accent/12 text-ink' : 'hover:bg-accent/5'}`}
              >
                <span
                  className={`absolute top-3.5 left-2 h-2 w-2 rounded-full ${ORIGINATOR_COLOR[d.originator_type]}`}
                />
                <span className="block truncate font-medium">{d.title}</span>
                <span className="flex items-center gap-1.5 font-mono text-[10px] text-muted">
                  {formatShortDate(d.issued_date ?? d.received_date) || '—'}
                  {d.significance_tier === 'critical' && <span className="text-danger">⚑</span>}
                  {d.thread_open && <span className="text-warning">open</span>}
                  {d.is_new && <span className="text-info">new</span>}
                  {d.needs_review && <span className="text-warning">review</span>}
                  {d.summary_pending && <span className="text-info">summary</span>}
                </span>
              </button>
            </li>
          ))}
          {docs.length === 0 && (
            <li className="px-3 py-6 text-center text-[12px] text-muted">
              {onlyOpen ? 'Nothing left to review.' : 'No documents in this proceeding yet.'}
            </li>
          )}
        </ul>
      </aside>
      <section className="min-w-0 flex-1 overflow-y-auto p-5">
        {selectedDoc ? (
          <DocumentReview docId={selectedDoc} onOpenHud={openDocument} />
        ) : (
          <p className="flex items-center gap-2 text-[12px] text-muted">
            <Icon name="description" size={16} /> Select a document from the spine.
          </p>
        )}
      </section>
    </div>
  )
}
