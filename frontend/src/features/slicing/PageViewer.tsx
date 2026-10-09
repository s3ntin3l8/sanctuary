import type { Schemas } from '../../api/client'
import { Icon } from '../../ui/Icon'

type Kind = Schemas['SliceCut']['kind']

type Props = {
  batchId: number
  page: number
  pages: Schemas['SlicingPage'][]
  /** Cuts keyed by the page they follow. */
  cuts: Map<number, Kind>
  onPage: (page: number) => void
  onClose: () => void
  onToggleCut: (after: number) => void
  onSetKind: (after: number, kind: Kind) => void
}

/** One page at reading size with the full OCR extract, so a cut can be judged and set in place. */
export function PageViewer({
  batchId,
  page,
  pages,
  cuts,
  onPage,
  onClose,
  onToggleCut,
  onSetKind,
}: Props) {
  const data = pages.find((p) => p.page === page)
  const total = pages.length
  const after = page - 1
  const kind = cuts.get(after)
  const part = [...cuts.keys()].filter((c) => c < page).length + 1

  return (
    <div
      role="dialog"
      aria-modal
      aria-label={`Page ${page} of ${total}`}
      className="fixed inset-0 z-140 flex flex-col bg-[rgba(6,14,32,.88)] backdrop-blur-[3px]"
    >
      <div className="flex items-center gap-3 border-b border-line bg-panel px-4 py-2 text-[12px]">
        <button
          type="button"
          aria-label="Previous page"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
          className="rounded-md p-1 hover:bg-accent/10 disabled:opacity-30"
        >
          <Icon name="chevron_left" size={20} />
        </button>
        <span className="font-mono">
          Page {page} / {total} · Part {part}
        </span>
        <button
          type="button"
          aria-label="Next page"
          disabled={page >= total}
          onClick={() => onPage(page + 1)}
          className="rounded-md p-1 hover:bg-accent/10 disabled:opacity-30"
        >
          <Icon name="chevron_right" size={20} />
        </button>
        <span className="font-mono text-[10px] text-muted">
          ←/→ flip · C cut before this page · L / A letter / attachment · Esc close
        </span>
        <button
          type="button"
          aria-label="Close viewer"
          onClick={onClose}
          className="ml-auto rounded-md p-1 hover:bg-accent/10"
        >
          <Icon name="close" size={18} />
        </button>
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 items-center justify-center overflow-auto p-4">
          <img
            src={`/api/v1/slicing/${batchId}/page/${page}`}
            alt={`Page ${page}`}
            className="max-h-full max-w-full rounded-md border border-line2 bg-white object-contain"
          />
        </div>
        <aside className="w-[340px] shrink-0 space-y-3 overflow-y-auto border-l border-line bg-panel p-4 text-[11px]">
          {page > 1 ? (
            <div>
              <button
                type="button"
                aria-pressed={kind !== undefined}
                onClick={() => onToggleCut(after)}
                className={`flex w-full items-center gap-2 rounded-md border px-3 py-1.5 text-[11px] ${kind ? 'border-accent bg-accent/12 text-ink' : 'border-line3 text-muted hover:text-ink'}`}
              >
                <Icon name="content_cut" size={14} />{' '}
                {kind ? 'A new part starts on this page' : 'Start a new part on this page'}
              </button>
              {kind && (
                <div role="group" aria-label="This part is" className="mt-1 flex">
                  {(['letter', 'attachment'] as const).map((k) => (
                    <button
                      key={k}
                      type="button"
                      aria-pressed={kind === k}
                      onClick={() => onSetKind(after, k)}
                      className={`flex-1 border px-2 py-1 first:rounded-l-md last:rounded-r-md ${kind === k ? 'border-accent bg-accent/12 text-ink' : 'border-line3 text-muted hover:text-ink'}`}
                    >
                      {k === 'letter' ? 'New letter' : 'Attachment'}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <p className="text-muted">The first page always starts the first part.</p>
          )}
          <section>
            <h2 className="mb-1 font-mono text-[10px] tracking-wider text-muted uppercase">
              Page starts
            </h2>
            <p className="whitespace-pre-wrap text-ink2">{data?.text_head || '—'}</p>
          </section>
          <section>
            <h2 className="mb-1 font-mono text-[10px] tracking-wider text-muted uppercase">
              Page ends
            </h2>
            <p className="whitespace-pre-wrap text-ink2">{data?.text_tail || '—'}</p>
          </section>
        </aside>
      </div>
    </div>
  )
}
