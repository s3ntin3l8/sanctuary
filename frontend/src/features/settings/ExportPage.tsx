import { buttonClass } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { SettingsCard } from '../../ui/SettingsCard'

export function ExportPage() {
  return (
    <SettingsCard
      title="Export your data"
      description="GDPR Art. 15/20: a complete, machine-readable copy of the workspace. One download per hour."
    >
      <ul className="list-disc space-y-0.5 pl-5 text-[12px] text-ink2">
        <li>Cases, proceedings, documents, relationships and claims (JSON Lines)</li>
        <li>Action items, costs, reactions, pins, conversations and the audit log</li>
        <li>Every original file and extraction under the data directory</li>
        <li>A manifest with row counts and the export date</li>
      </ul>
      <a href="/api/v1/settings/data/export" className={buttonClass('primary')} download>
        <Icon name="download" size={16} /> Download export zip
      </a>
    </SettingsCard>
  )
}
