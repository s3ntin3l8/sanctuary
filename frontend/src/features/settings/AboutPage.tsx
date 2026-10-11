import { useAbout } from '../../api/shell'
import { SettingsCard } from '../../ui/SettingsCard'
import { QueryState } from '../../ui/QueryState'

export function AboutPage() {
  const query = useAbout()
  const about = query.data
  if (!about) return <QueryState error={query.error} pending={query.isPending} />
  const rows = [
    ['Version', about.version],
    ['Commit', about.git_sha],
    ['Built', about.build_date],
    ['Database revision', about.db_revision ?? 'unknown'],
  ] as const
  return (
    <SettingsCard title="The Sanctuary" description="Privacy-first legal case management.">
      <dl className="grid grid-cols-[9rem_1fr] gap-y-2 text-[12px]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="font-mono break-all">{value}</dd>
          </div>
        ))}
      </dl>
    </SettingsCard>
  )
}
