import { useBriefing, useRefreshBriefing } from '../../api/home'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'

const time = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit' })

/**
 * The prototype's "Morning briefing": a few sentences from the local model
 * about what needs the user today, generated once per day and cached.
 */
export function BriefingCard() {
  const briefing = useBriefing()
  const refresh = useRefreshBriefing()
  const view = briefing.data
  if (!view) return null

  const busy = view.status === 'processing' || refresh.isPending
  return (
    <section
      aria-label="Morning briefing"
      aria-busy={busy}
      className="rounded-xl border border-accent/25 bg-aibg px-5 py-4"
    >
      <div className="flex items-center gap-2">
        <Icon name="auto_awesome" size={16} className="text-accent" />
        <span className="font-display text-[14px] font-bold">Morning briefing</span>
        <span className="ml-auto flex items-center gap-1 font-mono text-[10px] text-muted">
          <Icon name="lock" size={12} /> generated locally
          {view.generated_at && ` · ${time.format(new Date(view.generated_at))}`}
          {view.model_label && ` · ${view.model_label}`}
        </span>
        <Button
          variant="secondary"
          className="px-2 py-1 text-[11px]"
          disabled={busy}
          onClick={() => refresh.mutate()}
          aria-label="Regenerate briefing"
          title="Regenerate"
        >
          <Icon name="refresh" size={14} />
        </Button>
      </div>
      {view.status === 'processing' && (
        <p className="mt-2 text-[12.5px] text-muted">
          Reading today's deadlines, inbox and signals…
        </p>
      )}
      {view.status === 'failed' && (
        <p role="alert" className="mt-2 text-[12.5px] text-danger">
          The briefing could not be generated{view.error ? `: ${view.error}` : '.'}
        </p>
      )}
      {view.status === 'ready' && (
        <>
          <p className="mt-2 text-[13px] leading-relaxed text-ink">{view.summary}</p>
          {view.priorities.length > 0 && (
            <ol className="mt-2 list-decimal space-y-0.5 pl-5 text-[12px] text-ink2">
              {view.priorities.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  )
}
