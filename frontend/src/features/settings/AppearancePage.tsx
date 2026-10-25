import { useShell } from '../../api/shell'
import { useAppearance, useSaveDashboardCards, useSaveTimezone } from '../../api/settings'
import { useThemeControl } from '../../theme'
import { Button } from '../../ui/Button'
import { Field } from '../../ui/Field'
import { Icon } from '../../ui/Icon'
import { SettingsCard } from '../../ui/SettingsCard'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

const THEMES = [
  ['system', 'System', 'contrast'],
  ['dark', 'Dark', 'dark_mode'],
  ['light', 'Light', 'light_mode'],
] as const

const CARDS = [
  ['action_items', 'Action items & deadlines'],
  ['costs', 'Legal costs'],
  ['documents', 'Recent documents'],
] as const

export function AppearancePage() {
  const viewQuery = useAppearance()
  const view = viewQuery.data
  const isAdmin = useShell().data?.user.role === 'admin'
  const { pref, theme, choose } = useThemeControl()
  const saveCards = useSaveDashboardCards()
  const saveTz = useSaveTimezone()
  const toast = useToast()
  if (!view) return <QueryState error={viewQuery.error} pending={viewQuery.isPending} />

  return (
    <>
      <SettingsCard title="Theme">
        <div className="flex gap-2">
          {THEMES.map(([t, label, icon]) => (
            <Button
              key={t}
              variant={pref === t ? 'primary' : 'secondary'}
              aria-pressed={pref === t}
              onClick={() => choose(t)}
            >
              <Icon name={icon} size={16} /> {label}
            </Button>
          ))}
        </div>
        {pref === 'system' && (
          <p className="text-[12px] text-muted">Following your system: currently {theme}.</p>
        )}
      </SettingsCard>
      <SettingsCard title="Dashboard panels" description="Panels shown on the case dashboard.">
        {CARDS.map(([key, label]) => (
          <label key={key} className="flex items-center gap-2 text-[12.5px]">
            <input
              type="checkbox"
              checked={view.dashboard_cards[key]}
              onChange={(e) =>
                saveCards.mutate({ ...view.dashboard_cards, [key]: e.target.checked })
              }
              className="h-4 w-4 accent-accent"
            />
            {label}
          </label>
        ))}
      </SettingsCard>
      <SettingsCard
        title="Timezone"
        description="Workspace-wide display timezone for dates and deadlines."
      >
        <Field label="Timezone" htmlFor="tz">
          <select
            id="tz"
            value={view.timezone}
            disabled={!isAdmin}
            onChange={(e) =>
              saveTz.mutate(
                { tz: e.target.value },
                { onError: (err) => toast(err.message, 'error') },
              )
            }
            className="rounded-[9px] border border-line bg-panel2 px-2 py-1.5 text-[12px] disabled:opacity-60"
          >
            {view.timezone_choices.map((tz) => (
              <option key={tz} value={tz}>
                {tz}
              </option>
            ))}
          </select>
        </Field>
        {!isAdmin && (
          <p className="text-[11px] text-muted">
            Only an administrator can change the workspace timezone.
          </p>
        )}
      </SettingsCard>
    </>
  )
}
