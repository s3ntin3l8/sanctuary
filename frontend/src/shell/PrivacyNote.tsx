import { Link } from 'react-router'

import { useShell } from '../api/shell'
import { Icon } from '../ui/Icon'

/**
 * Where document text goes. "Local" only while every AI endpoint in use is on this machine or
 * the local network; an external one (a cloud chat model, the opt-in verifier) is named instead,
 * with a way to the settings that decide it.
 */
export function PrivacyNote({
  onNavigate,
  lowercase = false,
}: {
  onNavigate?: () => void
  /** The settings header writes it in lower case. */
  lowercase?: boolean
}) {
  const external = useShell().data?.ai_external ?? false
  if (!external) {
    return (
      <>
        <Icon name="lock" size={12} /> {lowercase ? 'local' : 'Local'} · nothing leaves this device
      </>
    )
  }
  return (
    <Link
      to="/settings/ai"
      onClick={onNavigate}
      className="inline-flex items-center gap-1.5 text-warning hover:underline"
    >
      <Icon name="public" size={12} /> {lowercase ? 'external' : 'External'} AI endpoint in use ·
      document text leaves this device
    </Link>
  )
}
